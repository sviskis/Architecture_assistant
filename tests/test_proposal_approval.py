"""Tests for the proposal approval use-case (Step 27).

``ProposalApproval`` is the only place a human decision on a **managed-project**
architecture proposal is recorded, and these tests pin both halves of that
sentence.

*Which state*: ``DRAFT -> APPROVED``, ``DRAFT -> REJECTED`` and
``DRAFT -> REVISION_REQUESTED`` - and nothing else. A proposal that was already
decided, that a newer revision replaced, that belongs to another project, or
whose stored content no longer matches its own fingerprint can never be approved;
the refusal happens before the transaction, so a refused decision changes neither
the row nor the audit trail.

*Which boundary*: approving a managed project's design must not move the
assistant's own architecture. The last tests therefore assert the *absence* of
coupling in the module's code and prove, against real SQLite, that
``architecture_versions``, ``architecture_change_requests``, ``adrs`` and
``risks`` are byte-identical after every decision the use-case can take.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from architecture_assistant.application import (
    ArchitectureSynthesis,
    ProposalActorRequiredError,
    ProposalApproval,
    ProposalApprovalError,
    ProposalDecisionConflictError,
    ProposalFeedbackRequiredError,
    ProposalNotApprovableError,
    ProposalNotFoundError,
    ProposalReasonRequiredError,
)
from architecture_assistant.domain.audit import (
    AuditAction,
    AuditEntityType,
    AuditEntry,
)
from architecture_assistant.domain.enums import ProposalStatus
from architecture_assistant.domain.models import ArchitectureProposal, Project
from architecture_assistant.infrastructure import (
    SqliteStorage,
    close_database,
    open_database,
)

NOW = datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 21, 23, 0, tzinfo=timezone.utc)

ACTOR = "gints"
REASON = "the operator accepted the design"
FEEDBACK = "split the transcriber into its own module"
PROJECT = "youtube_to_mp3"

#: The module under test - read by the boundary tests at the bottom.
MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "architecture_assistant"
    / "application"
    / "proposal_approval.py"
)


def fixed_clock(stamp: datetime = LATER):
    """A deterministic clock (never the system time)."""

    def clock() -> datetime:
        return stamp

    return clock


def seed_project(store: SqliteStorage, name: str = PROJECT) -> None:
    """The one project row a production bootstrap would have created."""
    store.projects.upsert(
        Project(name=name, plan_version="1.0", plan_hash="0" * 64)
    )


@contextmanager
def fake_transaction():
    """A transaction boundary that commits nothing - membership is the point."""
    yield


class FakeProposalRepository:
    """An in-memory proposal repository with the port's exact contract."""

    def __init__(self, items: tuple[ArchitectureProposal, ...] = ()) -> None:
        self.items: dict[str, ArchitectureProposal] = {
            item.proposal_id: item for item in items
        }

    def upsert(self, proposal: ArchitectureProposal) -> None:
        self.items[proposal.proposal_id] = proposal

    def get(self, proposal_id: str) -> Optional[ArchitectureProposal]:
        return self.items.get(proposal_id)

    def list(self) -> tuple[ArchitectureProposal, ...]:
        return tuple(self.items.values())

    def list_by_status(self, status: ProposalStatus):
        return tuple(
            item for item in self.items.values() if item.status is status
        )

    def list_for_project(self, project: str):
        return tuple(
            item for item in self.items.values() if item.project == project
        )

    def delete(self, proposal_id: str) -> bool:
        return self.items.pop(proposal_id, None) is not None


class FakeProjectRepository:
    """The one-project read the approval path performs."""

    def __init__(self, names: tuple[str, ...] = ("youtube_to_mp3",)) -> None:
        self.names = names
        self.reads = 0

    def list(self):
        self.reads += 1

        class Row:
            def __init__(self, name: str) -> None:
                self.name = name

        return tuple(Row(name) for name in self.names)


class FakeAudit:
    """An append-only audit trail in memory."""

    def __init__(self, *, fail: bool = False) -> None:
        self.entries: list[AuditEntry] = []
        self.fail = fail

    def append(self, entry: AuditEntry) -> None:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.entries.append(entry)


class _Transactions:
    """The transaction boundary the fakes are handed."""

    @contextmanager
    def transaction(self):
        yield


def review_payload(**overrides: Any) -> dict[str, Any]:
    """One complete review payload - the evidence a draft is built from."""
    payload: dict[str, Any] = {
        "review_id": "architecture-review-9-abcdef012345",
        "reviewed_at": "2026-09-21T22:00:00+00:00",
        "question": "MP3 -> TXT. Windows Python GUI.",
        "project": PROJECT,
        "source_root": "src/architecture_assistant",
        "check_source_root": "src/architecture_assistant",
        "source_root_verified": True,
        "step_no": 3,
        "architecture_version": "1.1",
        "deterministic_gate": {
            "available": True,
            "compliant": True,
            "baseline_version": "1.1",
            "violation_count": 0,
            "decision_id": "realization-3-1",
        },
        "providers": [
            {
                "source": "openai",
                "status": "FINDING",
                "reason": "",
                "finding": {
                    "id": "finding-openai",
                    "source": "openai",
                    "claim": "split the pipeline into stages",
                    "evidence": ["application/x.py:1"],
                    "confidence": 0.6,
                    "severity": "MEDIUM",
                },
                "relation": None,
                "relation_target": None,
                "cost": {},
            },
            {
                "source": "claude",
                "status": "ABSTAIN",
                "reason": "no api key was configured",
                "finding": None,
                "relation": None,
                "relation_target": None,
                "cost": {},
            },
            {
                "source": "grok",
                "status": "ERROR",
                "reason": "transport error",
                "finding": None,
                "relation": None,
                "relation_target": None,
                "cost": {},
            },
        ],
        "evidence": {
            "finding_ids": ["finding-openai"],
            "unresolved_questions": ["Which intermediate audio format?"],
            "gate_status": "compliant",
        },
        "conflicts": [],
        "judge": {
            "available": True,
            "consulted": False,
            "status": "NOT_CONSULTED",
        },
        "decision": {"status": "PENDING", "decision": ""},
        "cost": {"total": {"available": True, "total_usd": 0.02}},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def storage():
    """The real store, with the single managed project it always holds.

    A production database gets its project row from the composition bootstrap; a
    bare in-memory database has none, so the fixture writes exactly one - which is
    also what makes the project-identity check in ``approve`` meaningful.
    """
    connection = open_database(":memory:")
    try:
        store = SqliteStorage(connection)
        seed_project(store)
        yield store
    finally:
        close_database(connection)


class Harness:
    """The two use-cases over the real store, wired like the composition root."""

    def __init__(self, storage: SqliteStorage, *, clock: Any = None) -> None:
        self.storage = storage
        self.clock = clock if clock is not None else fixed_clock()
        self.project = storage.projects.list()[0].name
        self.synthesis = ArchitectureSynthesis(
            storage.proposals,
            storage.audit,
            storage,
            clock=self.clock,
        )
        self.approval = ProposalApproval(
            storage.proposals,
            storage.projects,
            storage.audit,
            storage,
            clock=self.clock,
        )

    def draft(self, **kwargs: Any) -> ArchitectureProposal:
        """One ``DRAFT`` proposal for *this* database's managed project."""
        project = kwargs.pop("project", self.project)
        return self.synthesis.synthesize(
            review_payload(project=project),
            actor=ACTOR,
            reason=REASON,
            **kwargs,
        )

    def audit_entries(self) -> list[AuditEntry]:
        """Only the proposal entries - the assistant's own trail is not ours."""
        return [
            entry
            for entry in self.storage.audit.list()
            if entry.entity_type is AuditEntityType.PROPOSAL
        ]

    def assistant_snapshot(self) -> tuple[Any, ...]:
        return (
            tuple(
                item.to_dict()
                for item in self.storage.architecture_versions.list()
            ),
            tuple(
                item.to_dict() for item in self.storage.change_requests.list()
            ),
            tuple(item.to_dict() for item in self.storage.adrs.list()),
            tuple(item.to_dict() for item in self.storage.risks.list()),
        )


@pytest.fixture
def harness(storage: SqliteStorage) -> Harness:
    return Harness(storage)


def fake_approval(
    proposal: ArchitectureProposal,
    *,
    project: str = PROJECT,
    audit: Any = None,
) -> tuple[ProposalApproval, FakeProposalRepository, FakeAudit]:
    """An approval over the in-memory fakes (for the pure rules)."""
    proposals = FakeProposalRepository((proposal,))
    ledger = audit if audit is not None else FakeAudit()
    return (
        ProposalApproval(
            proposals,
            FakeProjectRepository((project,)),
            ledger,
            _Transactions(),
            clock=fixed_clock(),
        ),
        proposals,
        ledger,
    )


class TestApproval:
    """``DRAFT -> APPROVED``: the managed project has an approved design."""

    def test_approval_records_the_human_decision(self, harness: Harness) -> None:
        draft = harness.draft()

        approved = harness.approval.approve(
            draft.proposal_id, actor=ACTOR, reason=REASON
        )

        assert approved.status is ProposalStatus.APPROVED
        assert approved.decided_by == ACTOR
        assert approved.decided_at == LATER
        assert approved.decision_reason == REASON
        # everything else about the proposal is untouched
        assert approved.fingerprint == draft.fingerprint
        assert approved.review_digest == draft.review_digest
        assert approved.requirement == draft.requirement

    def test_approval_is_persisted(self, harness: Harness) -> None:
        draft = harness.draft()

        harness.approval.approve(draft.proposal_id, actor=ACTOR, reason=REASON)

        stored = harness.storage.proposals.get(draft.proposal_id)
        assert stored is not None
        assert stored.status is ProposalStatus.APPROVED
        assert stored.decided_by == ACTOR

    def test_an_actor_is_required(self, harness: Harness) -> None:
        draft = harness.draft()

        with pytest.raises(ProposalActorRequiredError):
            harness.approval.approve(draft.proposal_id, actor="   ", reason=REASON)

        assert harness.storage.proposals.get(draft.proposal_id).status is (
            ProposalStatus.DRAFT
        )
        assert len(harness.audit_entries()) == 1

    def test_a_reason_is_required(self, harness: Harness) -> None:
        draft = harness.draft()

        with pytest.raises(ProposalReasonRequiredError):
            harness.approval.approve(draft.proposal_id, actor=ACTOR, reason="")

        assert harness.storage.proposals.get(draft.proposal_id).status is (
            ProposalStatus.DRAFT
        )
        assert len(harness.audit_entries()) == 1

    def test_an_unknown_proposal_id_is_refused(self, harness: Harness) -> None:
        with pytest.raises(ProposalNotFoundError):
            harness.approval.approve(
                "proposal-does-not-exist", actor=ACTOR, reason=REASON
            )

        assert harness.audit_entries() == []

    def test_a_blank_proposal_id_is_refused(self, harness: Harness) -> None:
        with pytest.raises(ProposalApprovalError):
            harness.approval.approve("", actor=ACTOR, reason=REASON)

    def test_approval_targets_exactly_one_proposal_id(
        self, harness: Harness
    ) -> None:
        first = harness.draft()
        second = harness.synthesis.synthesize(
            review_payload(question="a second requirement"),
            actor=ACTOR,
            reason=REASON,
        )

        harness.approval.approve(first.proposal_id, actor=ACTOR, reason=REASON)

        assert harness.storage.proposals.get(first.proposal_id).status is (
            ProposalStatus.APPROVED
        )
        assert harness.storage.proposals.get(second.proposal_id).status is (
            ProposalStatus.DRAFT
        )

    def test_one_approval_writes_exactly_one_audit_entry(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()

        harness.approval.approve(draft.proposal_id, actor=ACTOR, reason=REASON)

        entries = harness.audit_entries()
        assert len(entries) == 2  # the generation + this decision
        decision = entries[-1]
        assert decision.action is AuditAction.UPDATE
        assert decision.entity_id == draft.proposal_id
        assert decision.detail["operation"] == "approve-proposal"
        assert decision.detail["status_from"] == "DRAFT"
        assert decision.detail["status_to"] == "APPROVED"
        assert decision.detail["actor"] == ACTOR
        assert decision.detail["reason"] == REASON
        assert decision.detail["project"] == harness.project


class TestRejection:
    """``DRAFT -> REJECTED``: the design is refused, and only the proposal moves."""

    def test_rejection_is_recorded_with_the_reject_action(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()

        rejected = harness.approval.reject(
            draft.proposal_id, actor=ACTOR, reason="the operator refused it"
        )

        assert rejected.status is ProposalStatus.REJECTED
        assert rejected.decided_by == ACTOR
        assert rejected.decision_reason == "the operator refused it"
        entry = harness.audit_entries()[-1]
        assert entry.action is AuditAction.REJECT
        assert entry.detail["operation"] == "reject-proposal"
        assert entry.detail["status_to"] == "REJECTED"

    def test_rejection_creates_no_revision_and_no_second_proposal(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()

        harness.approval.reject(draft.proposal_id, actor=ACTOR, reason="no")

        assert len(harness.storage.proposals.list()) == 1
        assert harness.storage.proposals.get(draft.proposal_id).revision_no == 1

    def test_rejection_requires_an_actor_and_a_reason(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()

        with pytest.raises(ProposalActorRequiredError):
            harness.approval.reject(draft.proposal_id, actor="", reason="no")
        with pytest.raises(ProposalReasonRequiredError):
            harness.approval.reject(draft.proposal_id, actor=ACTOR, reason=" ")

        assert len(harness.audit_entries()) == 1

    def test_a_rejected_proposal_can_never_be_approved(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()
        harness.approval.reject(draft.proposal_id, actor=ACTOR, reason="no")

        with pytest.raises(ProposalNotApprovableError):
            harness.approval.approve(
                draft.proposal_id, actor=ACTOR, reason="changed my mind"
            )

        assert harness.storage.proposals.get(draft.proposal_id).status is (
            ProposalStatus.REJECTED
        )


class TestRevisionRequest:
    """``DRAFT -> REVISION_REQUESTED``: feedback, and never an auto re-synthesis."""

    def test_a_revision_request_records_the_feedback(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()

        asked = harness.approval.request_revision(
            draft.proposal_id,
            actor=ACTOR,
            reason="the operator wants changes",
            feedback=FEEDBACK,
        )

        assert asked.status is ProposalStatus.REVISION_REQUESTED
        assert asked.revision_feedback == FEEDBACK
        assert asked.decided_by == ACTOR
        entry = harness.audit_entries()[-1]
        assert entry.action is AuditAction.UPDATE
        assert entry.detail["operation"] == "request-proposal-revision"
        assert entry.detail["revision_feedback"] == FEEDBACK

    def test_feedback_is_required(self, harness: Harness) -> None:
        draft = harness.draft()

        with pytest.raises(ProposalFeedbackRequiredError):
            harness.approval.request_revision(
                draft.proposal_id,
                actor=ACTOR,
                reason="please change it",
                feedback="   ",
            )

        assert harness.storage.proposals.get(draft.proposal_id).status is (
            ProposalStatus.DRAFT
        )
        assert len(harness.audit_entries()) == 1

    def test_a_revision_request_never_re_synthesizes(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()

        harness.approval.request_revision(
            draft.proposal_id,
            actor=ACTOR,
            reason="changes",
            feedback=FEEDBACK,
        )

        # still exactly one proposal: a new revision needs an explicit Generate
        assert len(harness.storage.proposals.list()) == 1
        assert len(harness.audit_entries()) == 2

    def test_the_next_explicit_revision_carries_the_feedback(
        self, harness: Harness
    ) -> None:
        first = harness.draft()
        harness.approval.request_revision(
            first.proposal_id,
            actor=ACTOR,
            reason="changes",
            feedback=FEEDBACK,
        )

        second = harness.draft(revision_of=first.proposal_id)

        assert second.revision_no == 2
        assert second.revision_of == first.proposal_id
        assert harness.storage.proposals.get(first.proposal_id).status is (
            ProposalStatus.SUPERSEDED
        )
        assert harness.storage.proposals.get(first.proposal_id).superseded_by == (
            second.proposal_id
        )

    def test_a_revision_requested_proposal_can_never_be_approved(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()
        harness.approval.request_revision(
            draft.proposal_id,
            actor=ACTOR,
            reason="changes",
            feedback=FEEDBACK,
        )

        with pytest.raises(ProposalNotApprovableError):
            harness.approval.approve(
                draft.proposal_id, actor=ACTOR, reason="approve anyway"
            )


class TestStaleness:
    """A design that has moved on can never be approved by mistake."""

    def test_a_superseded_proposal_cannot_be_approved(
        self, harness: Harness
    ) -> None:
        first = harness.draft()
        second = harness.draft(revision_of=first.proposal_id)

        with pytest.raises(ProposalNotApprovableError):
            harness.approval.approve(
                first.proposal_id, actor=ACTOR, reason="approve the old one"
            )

        assert harness.storage.proposals.get(first.proposal_id).status is (
            ProposalStatus.SUPERSEDED
        )
        assert harness.storage.proposals.get(second.proposal_id).status is (
            ProposalStatus.DRAFT
        )

    def test_a_proposal_replaced_by_a_revision_cannot_be_approved(
        self, harness: Harness
    ) -> None:
        """Lineage alone is enough: a newer revision makes the old one stale."""
        first = harness.draft()
        second = harness.draft(revision_of=first.proposal_id)
        # simulate a row whose own supersession marker was never written
        stored = harness.storage.proposals.get(first.proposal_id)
        harness.storage.proposals.upsert(
            replace(stored, status=ProposalStatus.DRAFT, superseded_by=None)
        )

        with pytest.raises(ProposalNotApprovableError):
            harness.approval.approve(
                first.proposal_id, actor=ACTOR, reason="approve the old one"
            )

        assert harness.storage.proposals.get(second.proposal_id).status is (
            ProposalStatus.DRAFT
        )

    def test_the_stale_head_is_still_decidable(self, harness: Harness) -> None:
        first = harness.draft()
        second = harness.draft(revision_of=first.proposal_id)

        approved = harness.approval.approve(
            second.proposal_id, actor=ACTOR, reason="approve the new one"
        )

        assert approved.status is ProposalStatus.APPROVED

    def test_a_proposal_of_another_project_cannot_be_approved(
        self, harness: Harness
    ) -> None:
        foreign = harness.draft(project="some_other_project")

        with pytest.raises(ProposalNotApprovableError):
            harness.approval.approve(
                foreign.proposal_id, actor=ACTOR, reason="approve it"
            )

        assert harness.storage.proposals.get(foreign.proposal_id).status is (
            ProposalStatus.DRAFT
        )

    def test_a_proposal_whose_inputs_no_longer_match_its_fingerprint_is_refused(
        self, harness: Harness
    ) -> None:
        """The fingerprint identifies the *inputs* a proposal was built from."""
        draft = harness.draft()
        harness.storage.proposals.upsert(
            replace(draft, requirement="a requirement nobody approved")
        )

        with pytest.raises(ProposalNotApprovableError):
            harness.approval.approve(
                draft.proposal_id, actor=ACTOR, reason="approve it"
            )

    def test_a_proposal_with_a_tampered_digest_is_refused(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()
        digest = dict(draft.review_digest)
        digest["finding_count"] = 99
        harness.storage.proposals.upsert(replace(draft, review_digest=digest))

        with pytest.raises(ProposalNotApprovableError):
            harness.approval.approve(
                draft.proposal_id, actor=ACTOR, reason="approve it"
            )


    def test_a_stale_refusal_writes_nothing(self, harness: Harness) -> None:
        first = harness.draft()
        harness.draft(revision_of=first.proposal_id)
        before = harness.storage.proposals.get(first.proposal_id)
        audit_before = len(harness.audit_entries())

        with pytest.raises(ProposalNotApprovableError):
            harness.approval.approve(first.proposal_id, actor=ACTOR, reason="x")

        assert harness.storage.proposals.get(first.proposal_id) == before
        assert len(harness.audit_entries()) == audit_before


class TestIdempotence:
    """Repeating the *same* decision is a no-op; a different one fails closed."""

    def test_repeating_the_identical_approval_writes_no_second_entry(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()
        first = harness.approval.approve(
            draft.proposal_id, actor=ACTOR, reason=REASON
        )
        entries = len(harness.audit_entries())

        again = harness.approval.approve(
            draft.proposal_id, actor=ACTOR, reason=REASON
        )

        assert again == first
        assert len(harness.audit_entries()) == entries

    def test_approving_again_with_another_reason_fails_closed(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()
        harness.approval.approve(draft.proposal_id, actor=ACTOR, reason=REASON)
        entries = len(harness.audit_entries())

        with pytest.raises(ProposalDecisionConflictError):
            harness.approval.approve(
                draft.proposal_id, actor=ACTOR, reason="a different reason"
            )

        assert len(harness.audit_entries()) == entries
        assert (
            harness.storage.proposals.get(draft.proposal_id).decision_reason
            == REASON
        )

    def test_approving_again_as_another_actor_fails_closed(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()
        harness.approval.approve(draft.proposal_id, actor=ACTOR, reason=REASON)

        with pytest.raises(ProposalDecisionConflictError):
            harness.approval.approve(
                draft.proposal_id, actor="someone else", reason=REASON
            )

    def test_repeating_the_identical_rejection_writes_no_second_entry(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()
        harness.approval.reject(draft.proposal_id, actor=ACTOR, reason="no")
        entries = len(harness.audit_entries())

        again = harness.approval.reject(
            draft.proposal_id, actor=ACTOR, reason="no"
        )

        assert again.status is ProposalStatus.REJECTED
        assert len(harness.audit_entries()) == entries

    def test_repeating_the_identical_revision_request_is_idempotent(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()
        harness.approval.request_revision(
            draft.proposal_id,
            actor=ACTOR,
            reason="changes",
            feedback=FEEDBACK,
        )
        entries = len(harness.audit_entries())

        again = harness.approval.request_revision(
            draft.proposal_id,
            actor=ACTOR,
            reason="changes",
            feedback=FEEDBACK,
        )

        assert again.status is ProposalStatus.REVISION_REQUESTED
        assert len(harness.audit_entries()) == entries

    def test_a_different_revision_feedback_fails_closed(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()
        harness.approval.request_revision(
            draft.proposal_id,
            actor=ACTOR,
            reason="changes",
            feedback=FEEDBACK,
        )

        with pytest.raises(ProposalDecisionConflictError):
            harness.approval.request_revision(
                draft.proposal_id,
                actor=ACTOR,
                reason="changes",
                feedback="something else entirely",
            )

    def test_a_second_different_decision_is_refused(
        self, harness: Harness
    ) -> None:
        draft = harness.draft()
        harness.approval.approve(draft.proposal_id, actor=ACTOR, reason=REASON)

        with pytest.raises(ProposalNotApprovableError):
            harness.approval.reject(draft.proposal_id, actor=ACTOR, reason="no")


class TestPersistence:
    """Atomicity, restart safety and the assistant's untouched state."""

    def test_a_failing_audit_write_rolls_the_decision_back(
        self, storage: SqliteStorage
    ) -> None:
        harness = Harness(storage)
        draft = harness.draft()
        harness.approval._audit = FakeAudit(fail=True)

        with pytest.raises(RuntimeError):
            harness.approval.approve(
                draft.proposal_id, actor=ACTOR, reason=REASON
            )

        stored = storage.proposals.get(draft.proposal_id)
        assert stored is not None
        assert stored.status is ProposalStatus.DRAFT
        assert stored.decided_by == ""

    def test_an_approved_proposal_survives_a_restart(self, tmp_path) -> None:
        path = str(tmp_path / "assistant.db")
        connection = open_database(path)
        try:
            storage = SqliteStorage(connection)
            seed_project(storage)
            harness = Harness(storage)
            draft = harness.draft()
            harness.approval.approve(
                draft.proposal_id, actor=ACTOR, reason=REASON
            )
        finally:
            close_database(connection)

        reopened = open_database(path)
        try:
            storage = SqliteStorage(reopened)
            stored = storage.proposals.get(draft.proposal_id)
            assert stored is not None
            assert stored.status is ProposalStatus.APPROVED
            assert stored.decided_by == ACTOR
            assert stored.decided_at == LATER
            assert stored.decision_reason == REASON
            # and it is still decidable as *already* decided, not as a draft
            approval = ProposalApproval(
                storage.proposals,
                storage.projects,
                storage.audit,
                storage,
                clock=fixed_clock(),
            )
            assert (
                approval.approve(
                    draft.proposal_id, actor=ACTOR, reason=REASON
                ).status
                is ProposalStatus.APPROVED
            )
        finally:
            close_database(reopened)

    def test_no_decision_moves_the_assistants_own_architecture(
        self, harness: Harness
    ) -> None:
        approved = harness.draft()
        rejected = harness.synthesis.synthesize(
            review_payload(question="a second requirement"),
            actor=ACTOR,
            reason=REASON,
        )
        asked = harness.synthesis.synthesize(
            review_payload(question="a third requirement"),
            actor=ACTOR,
            reason=REASON,
        )
        before = harness.assistant_snapshot()

        harness.approval.approve(
            approved.proposal_id, actor=ACTOR, reason=REASON
        )
        harness.approval.reject(rejected.proposal_id, actor=ACTOR, reason="no")
        harness.approval.request_revision(
            asked.proposal_id,
            actor=ACTOR,
            reason="changes",
            feedback=FEEDBACK,
        )

        assert harness.assistant_snapshot() == before

    def test_approval_never_creates_a_change_request_or_a_version(
        self, harness: Harness
    ) -> None:
        drafts = harness.storage.change_requests.list()
        versions = harness.storage.architecture_versions.list()
        draft = harness.draft()

        harness.approval.approve(draft.proposal_id, actor=ACTOR, reason=REASON)

        assert harness.storage.change_requests.list() == drafts
        assert harness.storage.architecture_versions.list() == versions

    def test_the_wiring_is_four_collaborators_and_nothing_else(
        self, harness: Harness
    ) -> None:
        approval = harness.approval

        assert sorted(approval.__dict__) == [
            "_audit",
            "_clock",
            "_projects",
            "_proposals",
            "_transactions",
        ]

    def test_the_collaborators_are_validated(self) -> None:
        with pytest.raises(ValueError):
            ProposalApproval(object(), FakeProjectRepository(), object(), object())


def module_node() -> ast.Module:
    """The parsed source of the module under test."""
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


def referenced_names() -> set[str]:
    """Every identifier the module's *code* uses (docstrings are inert)."""
    names: set[str] = set()
    for node in ast.walk(module_node()):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def string_constants() -> set[str]:
    """Every string literal the module's *code* uses (docstrings excluded)."""
    tree = module_node()
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


class TestBoundary:
    """Approving a managed project's design is not an assistant change.

    These checks read the module's *code*, not its prose: the docstrings
    legitimately name what approval is deliberately not given, and naming a
    collaborator in a sentence is documentation, not coupling.
    """

    def test_the_module_imports_no_assistant_architecture_port(self) -> None:
        names = referenced_names()

        forbidden = (
            "architecture_evolution",
            "architecture_versioning",
            "adr_manager",
            "risk_manager",
            "realization_control",
            "infrastructure",
            "sqlite3",
        )
        assert not [
            name for name in names for bad in forbidden if bad in name.lower()
        ]

    def test_the_code_never_holds_an_assistant_use_case(self) -> None:
        names = referenced_names()

        for absent in (
            "ArchitectureEvolution",
            "ArchitectureVersioning",
            "ArchitectureChangeRequest",
            "ADRManager",
            "RiskManager",
            "RealizationControl",
            "RealizationControlUseCase",
        ):
            assert absent not in names, absent

    def test_the_code_never_references_an_assistant_scoped_repository(
        self,
    ) -> None:
        names = referenced_names() | string_constants()

        for absent in ("architecture_versions", "change_requests", "adrs"):
            assert absent not in names, absent

    def test_the_code_never_writes_an_assistant_artifact(self) -> None:
        names = referenced_names() | string_constants()

        for absent in (
            "add_risk",
            "record_risk",
            "create_adr",
            "write_adr",
            "request_change",
            "create_change_request",
            "add_version",
            "record_version",
            "synthesize",
            "re_synthesize",
        ):
            assert absent not in names, absent

    def test_the_decision_operations_are_the_documented_three(self) -> None:
        operations = {
            text
            for text in string_constants()
            if isinstance(text, str) and text.endswith("-proposal")
        }
        operations |= {
            text
            for text in string_constants()
            if isinstance(text, str)
            and text.startswith("request-proposal")
        }

        assert operations == {
            "approve-proposal",
            "reject-proposal",
            "request-proposal-revision",
        }
