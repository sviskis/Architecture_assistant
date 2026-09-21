"""Tests for managed-project architecture synthesis (Step 27).

These tests pin the boundary, not just the behaviour. The synthesis use-case
turns one advisory architecture review into one durable ``DRAFT`` proposal for
the project the assistant **manages** - and it must be impossible for that
proposal to touch the assistant's *own* architecture: no ``ArchitectureVersion``
mutation, no ACR, no assistant ADR, no assistant risk, no rule change and no
realization verdict. Several tests below therefore assert the *absence* of
coupling in the module source and in the wired composition, and the integration
tests assert that every assistant-scoped repository is byte-identical after a
proposal is generated.

The evidence rules are pinned too: exactly one review is consumed, the operator's
requirement is echoed verbatim, advisor findings/statuses/conflicts/questions are
preserved without any tally, and a synthesizer that answers badly is refused
rather than stored.
"""

from __future__ import annotations

import ast
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from architecture_assistant.application import (
    DETERMINISTIC_SOURCE,
    PROPOSAL_CONTENT_FIELDS,
    ArchitectureSynthesis,
    SynthesisActorRequiredError,
    SynthesisError,
    SynthesisInsufficientEvidenceError,
    SynthesisProviderError,
    SynthesisReasonRequiredError,
    SynthesisResponseError,
    fingerprint_matches,
    proposal_fingerprint,
)
from architecture_assistant.domain.audit import (
    AuditAction,
    AuditEntityType,
    AuditEntry,
)
from architecture_assistant.domain.enums import ProposalStatus
from architecture_assistant.domain.models import ArchitectureProposal
from architecture_assistant.infrastructure import (
    SqliteStorage,
    close_database,
    open_database,
)
from architecture_assistant.ports.capabilities import (
    SynthesisPort,
    SynthesisQuery,
    SynthesisResult,
)

NOW = datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 21, 23, 0, tzinfo=timezone.utc)

PROJECT = "youtube_to_mp3"
REQUIREMENT = "MP3 -> TXT. Windows Python GUI."
ACTOR = "gints"
REASON = "design the pilot"

#: The module under test - read by the boundary tests at the bottom.
MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "architecture_assistant"
    / "application"
    / "architecture_synthesis.py"
)


def fixed_clock(stamp: datetime = NOW):
    """A deterministic clock (never the system time)."""

    def clock() -> datetime:
        return stamp

    return clock


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
        self.upserts = 0

    def upsert(self, proposal: ArchitectureProposal) -> None:
        self.upserts += 1
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


class FakeAudit:
    """An append-only audit trail in memory."""

    def __init__(self, *, fail: bool = False) -> None:
        self.entries: list[AuditEntry] = []
        self.fail = fail

    def append(self, entry: AuditEntry) -> None:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.entries.append(entry)


class FakeSynthesizer:
    """A scripted, provider-neutral synthesizer (offline, deterministic)."""

    def __init__(self, content: Any = None) -> None:
        self.content = content
        self.queries: list[SynthesisQuery] = []
        self.failure: Optional[BaseException] = None

    def synthesize(self, query: SynthesisQuery) -> SynthesisResult:
        self.queries.append(query)
        if self.failure is not None:
            raise self.failure
        if self.content is None:
            return SynthesisResult(
                source="scripted", content=dict(query.skeleton)
            )
        return SynthesisResult(source="scripted", content=self.content)


def filled_content(**overrides: Any) -> dict[str, Any]:
    """A complete, valid answer for the closed proposal contract."""
    content: dict[str, Any] = {
        "summary": "Layered youtube_to_mp3: domain, application, adapters, gui.",
        "modules": [
            {
                "name": "domain",
                "responsibility": "pure transcription rules",
                "dependencies": "none",
                "boundary_notes": "no I/O",
            },
            {
                "name": "gui",
                "responsibility": "the Windows panel",
                "dependencies": "application",
                "boundary_notes": "no download logic",
            },
        ],
        "data_flows": [
            {
                "from": "gui",
                "to": "domain",
                "description": "one transcription request",
            }
        ],
        "external_dependencies": [
            {"name": "ffmpeg", "purpose": "audio", "impact": "binary on PATH"}
        ],
        "architecture_rules": ["every stage is its own module"],
        "proposed_rules": ["the transcript must be reproducible"],
        "risks": [
            {
                "severity": "MEDIUM",
                "probability": 0.3,
                "impact": "MEDIUM",
                "description": "network flakiness",
                "mitigation": "fail loudly",
            }
        ],
        "adr_candidates": [
            {
                "title": "Strict layers",
                "decision": "domain/application/adapters/gui",
                "rationale": "testable without a network",
                "recommended_status": "PROPOSED",
            }
        ],
        "implementation_phases": [
            {
                "phase": "ARCHITECTURE",
                "goal": "lay out the packages",
                "scope": "one step",
            }
        ],
        "unresolved_questions": ["Which intermediate audio format?"],
        "rationale": "The advisors agreed on the pipeline shape.",
    }
    content.update(overrides)
    return content


def stage(
    source: str,
    status: str,
    *,
    claim: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """One advisor stage exactly as the review payload carries it."""
    return {
        "source": source,
        "status": status,
        "reason": reason,
        "finding": (
            None
            if status != "FINDING"
            else {
                "id": f"finding-{source.lower()}",
                "source": source.lower(),
                "claim": claim,
                "evidence": ["application/x.py:1"],
                "confidence": 0.6,
                "severity": "MEDIUM",
            }
        ),
        "relation": None,
        "relation_target": None,
        "cost": {},
    }


def review_payload(**overrides: Any) -> dict[str, Any]:
    """One complete review payload - the only evidence synthesis may consume."""
    payload: dict[str, Any] = {
        "review_id": "architecture-review-9-abcdef012345",
        "reviewed_at": "2026-09-21T22:00:00+00:00",
        "question": REQUIREMENT,
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
            stage("openai", "FINDING", claim="split the pipeline into stages"),
            stage("claude", "ABSTAIN", reason="no api key was configured"),
            stage("grok", "ERROR", reason="transport error"),
        ],
        "evidence": {
            "finding_ids": ["finding-openai"],
            "supporting_ids": [],
            "conflicting_ids": [],
            "unresolved_ids": [],
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
def sqlite_storage():
    """The real store: only a real transaction can prove a real rollback."""
    connection = open_database(":memory:")
    yield SqliteStorage(connection)
    close_database(connection)


def build_synthesis(
    storage: SqliteStorage,
    *,
    synthesis: Any = None,
    clock: Any = None,
) -> ArchitectureSynthesis:
    """A synthesis wired exactly like the composition root wires it."""
    return ArchitectureSynthesis(
        storage.proposals,
        storage.audit,
        storage,
        clock=clock if clock is not None else fixed_clock(),
        synthesis=synthesis,
    )


def fake_synthesis(
    *, synthesizer: Any = None, audit: Any = None, clock: Any = None
) -> tuple[ArchitectureSynthesis, FakeProposalRepository, FakeAudit]:
    """A synthesis over the in-memory fakes (for the pure rules)."""
    proposals = FakeProposalRepository()
    ledger = audit if audit is not None else FakeAudit()
    return (
        ArchitectureSynthesis(
            proposals,
            ledger,
            _Transactions(),
            clock=clock if clock is not None else fixed_clock(),
            synthesis=synthesizer,
        ),
        proposals,
        ledger,
    )


class _Transactions:
    """The transaction boundary the fakes are handed (one context manager)."""

    @contextmanager
    def transaction(self):
        yield


class TestEvidenceRules:
    """One review in, one honest proposal out - and nothing invented."""

    def test_it_consumes_exactly_one_review_identity(self) -> None:
        engine, proposals, _audit = fake_synthesis()
        review = review_payload()

        proposal = engine.synthesize(review, actor=ACTOR, reason=REASON)

        assert proposal.source_review_id == review["review_id"]
        assert proposal.review_digest["review_id"] == review["review_id"]
        assert proposal.project == PROJECT
        assert len(proposals.items) == 1

    def test_it_accepts_the_review_object_not_only_a_payload(self) -> None:
        engine, _proposals, _audit = fake_synthesis()

        class Result:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload

            def to_dict(self) -> dict[str, Any]:
                return dict(self._payload)

        proposal = engine.synthesize(
            Result(review_payload()), actor=ACTOR, reason=REASON
        )

        assert proposal.source_review_id == "architecture-review-9-abcdef012345"

    def test_the_operator_requirement_is_preserved_verbatim(self) -> None:
        engine, _proposals, _audit = fake_synthesis()
        requirement = "MP3 -> TXT.\nWindows Python GUI.\nNo video output."

        proposal = engine.synthesize(
            review_payload(question="ignored question"),
            requirement=requirement,
            actor=ACTOR,
            reason=REASON,
        )

        assert proposal.requirement == requirement
        assert "\n" in proposal.requirement

    def test_the_review_question_is_the_default_requirement(self) -> None:
        engine, _proposals, _audit = fake_synthesis()

        proposal = engine.synthesize(
            review_payload(question=REQUIREMENT), actor=ACTOR, reason=REASON
        )

        assert proposal.requirement == REQUIREMENT

    def test_advisor_statuses_are_preserved_without_a_tally(self) -> None:
        engine, _proposals, _audit = fake_synthesis()

        proposal = engine.synthesize(
            review_payload(), actor=ACTOR, reason=REASON
        )

        digest = proposal.review_digest
        assert digest["provider_count"] == 3
        assert digest["finding_count"] == 1
        assert digest["abstain_count"] == 1
        assert digest["error_count"] == 1
        assert digest["finding_sources"] == ["openai"]
        # one finding out of three is *not* a majority: no winner is chosen
        assert proposal.modules == ()
        assert proposal.risks == ()

    def test_a_three_way_disagreement_produces_no_winner(self) -> None:
        """No voting: three contradictory findings stay three findings."""
        engine, _proposals, _audit = fake_synthesis()
        review = review_payload(
            providers=[
                stage("openai", "FINDING", claim="split into five stages"),
                stage("claude", "FINDING", claim="keep one module"),
                stage("grok", "FINDING", claim="use a plugin registry"),
            ],
            evidence={
                "finding_ids": ["a", "b", "c"],
                "unresolved_questions": ["Which decomposition?"],
                "gate_status": "compliant",
            },
        )

        proposal = engine.synthesize(review, actor=ACTOR, reason=REASON)

        claims = [
            item["finding"]["claim"]
            for item in proposal.review_digest["providers"]
        ]
        assert sorted(claims) == [
            "keep one module",
            "split into five stages",
            "use a plugin registry",
        ]
        assert proposal.review_digest["finding_count"] == 3
        assert proposal.modules == ()

    def test_unresolved_questions_are_preserved(self) -> None:
        engine, _proposals, _audit = fake_synthesis()
        review = review_payload(
            evidence={
                "finding_ids": [],
                "unresolved_questions": ["Which audio format?", "Which GUI?"],
                "gate_status": "compliant",
            }
        )

        proposal = engine.synthesize(review, actor=ACTOR, reason=REASON)

        assert "Which audio format?" in proposal.unresolved_questions
        assert "Which GUI?" in proposal.unresolved_questions

    def test_conflicts_and_the_gate_are_carried_for_traceability(self) -> None:
        engine, _proposals, _audit = fake_synthesis()
        review = review_payload(
            conflicts=[
                {"target": "gate", "supporting": ["a"], "contradicting": ["b"]}
            ]
        )

        proposal = engine.synthesize(review, actor=ACTOR, reason=REASON)

        assert len(proposal.review_digest["conflicts"]) == 1
        assert (
            proposal.review_digest["deterministic_gate"]["compliant"] is True
        )

    def test_judge_prose_never_becomes_architecture(self) -> None:
        """The judge is a third layer: it is carried, never turned into modules."""
        engine, _proposals, _audit = fake_synthesis()
        review = review_payload(
            judge={
                "available": True,
                "consulted": True,
                "status": "OK",
                "count": 1,
                "judgments": [
                    {
                        "conflict_id": "c1",
                        "decision": "add a caching module",
                        "rationale": "it is the safest reading",
                    }
                ],
            }
        )

        proposal = engine.synthesize(review, actor=ACTOR, reason=REASON)

        assert proposal.review_digest["judge"]["status"] == "OK"
        assert proposal.modules == ()
        assert "caching" not in proposal.summary

    def test_the_proposal_is_json_safe(self) -> None:
        engine, _proposals, _audit = fake_synthesis(
            synthesizer=FakeSynthesizer(filled_content())
        )

        proposal = engine.synthesize(
            review_payload(), actor=ACTOR, reason=REASON
        )

        encoded = json.dumps(proposal.to_dict(), sort_keys=True)
        assert isinstance(encoded, str)
        assert ArchitectureProposal.from_dict(json.loads(encoded)) == proposal

    def test_the_fingerprint_is_clock_free_and_content_sensitive(self) -> None:
        first = proposal_fingerprint(
            project=PROJECT,
            review_id="r1",
            architecture_version="1.1",
            requirement=REQUIREMENT,
            revision_no=1,
            digest={"a": 1},
        )
        again = proposal_fingerprint(
            project=PROJECT,
            review_id="r1",
            architecture_version="1.1",
            requirement=REQUIREMENT,
            revision_no=1,
            digest={"a": 1},
        )
        changed = proposal_fingerprint(
            project=PROJECT,
            review_id="r1",
            architecture_version="1.1",
            requirement=REQUIREMENT,
            revision_no=1,
            digest={"a": 2},
        )

        assert first == again
        assert first != changed

    def test_a_stored_proposal_matches_its_own_fingerprint(self) -> None:
        engine, _proposals, _audit = fake_synthesis()

        proposal = engine.synthesize(
            review_payload(), actor=ACTOR, reason=REASON
        )

        assert fingerprint_matches(proposal) is True

    def test_the_deterministic_path_needs_no_synthesizer(self) -> None:
        engine, _proposals, _audit = fake_synthesis()

        proposal = engine.synthesize(
            review_payload(), actor=ACTOR, reason=REASON
        )

        assert engine.has_synthesizer is False
        assert proposal.status is ProposalStatus.DRAFT
        assert proposal.summary.startswith("Managed-project architecture")
        assert proposal.unresolved_questions
        assert "deterministically" in proposal.rationale


class TestRefusal:
    """Every refusal happens before the transaction: nothing is stored."""

    def test_an_actor_is_required(self) -> None:
        engine, proposals, audit = fake_synthesis()

        with pytest.raises(SynthesisActorRequiredError):
            engine.synthesize(review_payload(), actor="   ", reason=REASON)

        assert proposals.items == {}
        assert audit.entries == []

    def test_a_reason_is_required(self) -> None:
        engine, proposals, audit = fake_synthesis()

        with pytest.raises(SynthesisReasonRequiredError):
            engine.synthesize(review_payload(), actor=ACTOR, reason="")

        assert proposals.items == {}
        assert audit.entries == []

    def test_insufficient_evidence_is_refused(self) -> None:
        engine, proposals, audit = fake_synthesis()

        with pytest.raises(SynthesisInsufficientEvidenceError):
            engine.synthesize(
                review_payload(question="  ", evidence={}),
                actor=ACTOR,
                reason=REASON,
            )

        assert proposals.items == {}
        assert audit.entries == []

    def test_an_incomplete_review_is_refused(self) -> None:
        engine, proposals, _audit = fake_synthesis()

        with pytest.raises(SynthesisError):
            engine.synthesize(
                {"review_id": "r1", "question": REQUIREMENT},
                actor=ACTOR,
                reason=REASON,
            )

        assert proposals.items == {}

    def test_a_review_without_a_project_identity_is_refused(self) -> None:
        engine, proposals, _audit = fake_synthesis()

        with pytest.raises(SynthesisError):
            engine.synthesize(
                review_payload(project="   "), actor=ACTOR, reason=REASON
            )

        assert proposals.items == {}

    def test_something_that_is_not_a_review_is_refused(self) -> None:
        engine, proposals, _audit = fake_synthesis()

        with pytest.raises(SynthesisError):
            engine.synthesize(42, actor=ACTOR, reason=REASON)

        assert proposals.items == {}

    @pytest.mark.parametrize(
        "content",
        [
            {"summary": "only one field"},  # every field must be declared
            filled_content(unknown_field="surprise"),  # closed contract
            filled_content(summary="   "),  # an empty summary says nothing
            filled_content(modules="domain"),  # a bare string is not a list
            filled_content(modules=[{"name": "domain"}]),  # missing fields
            filled_content(
                modules=[
                    {
                        "name": "domain",
                        "responsibility": "r",
                        "dependencies": "d",
                        "boundary_notes": "b",
                        "extra": "x",
                    }
                ]
            ),
            filled_content(
                implementation_phases=[
                    {"phase": "NOPE", "goal": "g", "scope": "s"}
                ]
            ),
            filled_content(unresolved_questions="Which format?"),
            filled_content(rationale=""),
        ],
    )
    def test_malformed_synthesis_fails_closed(self, content: Any) -> None:
        """A synthesizer that answers badly is refused, never stored."""
        engine, proposals, audit = fake_synthesis(
            synthesizer=FakeSynthesizer(content)
        )

        with pytest.raises(SynthesisResponseError):
            engine.synthesize(review_payload(), actor=ACTOR, reason=REASON)

        # no approvable proposal exists and no audit entry claims one does
        assert proposals.items == {}
        assert audit.entries == []
        assert engine.proposals() == ()

    def test_a_synthesizer_that_returns_nonsense_is_refused(self) -> None:
        """The port's own contract is enforced too, and still stores nothing."""

        class Broken:
            def synthesize(self, query: SynthesisQuery) -> Any:
                return None

        engine, proposals, audit = fake_synthesis(synthesizer=Broken())

        with pytest.raises(SynthesisResponseError):
            engine.synthesize(review_payload(), actor=ACTOR, reason=REASON)

        assert proposals.items == {}
        assert audit.entries == []


    def test_a_failing_synthesizer_is_reported(self) -> None:
        synthesizer = FakeSynthesizer()
        synthesizer.failure = RuntimeError("provider exploded")
        engine, proposals, audit = fake_synthesis(synthesizer=synthesizer)

        with pytest.raises(SynthesisProviderError):
            engine.synthesize(review_payload(), actor=ACTOR, reason=REASON)

        assert proposals.items == {}
        assert audit.entries == []

    def test_a_transaction_failure_leaves_no_partial_row(self) -> None:
        """A failure between the two writes must roll the first one back."""
        connection = open_database(":memory:")
        storage = SqliteStorage(connection)
        try:
            engine = ArchitectureSynthesis(
                storage.proposals,
                _FailingAudit(),  # the audit write fails inside the transaction
                storage,
                clock=fixed_clock(),
            )

            with pytest.raises(RuntimeError):
                engine.synthesize(review_payload(), actor=ACTOR, reason=REASON)

            assert storage.proposals.list() == ()
            assert storage.audit.list() == ()
        finally:
            close_database(connection)


class _FailingAudit:
    """An audit port that fails - to prove the proposal write rolls back."""

    def append(self, entry: AuditEntry) -> None:
        raise RuntimeError("audit unavailable")

    def list(self, limit: Optional[int] = None):
        return ()


class TestBoundary:
    """The proposal is the *managed* project's - never the assistant's own.

    These checks read the module's *code* (via the AST), not its prose: the
    docstrings legitimately name what synthesis is deliberately **not** given,
    and those sentences are documentation, not coupling.
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

    def test_the_code_never_references_an_assistant_scoped_repository(
        self,
    ) -> None:
        names = referenced_names() | string_constants()

        for absent in ("architecture_versions", "change_requests", "adrs"):
            assert absent not in names, absent

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

    def test_the_code_has_no_tallying_vocabulary(self) -> None:
        """No voting, no ranking: a tally is not even a concept here."""
        names = referenced_names() | string_constants()

        for absent in (
            "majority",
            "consensus",
            "tally",
            "quorum",
            "vote",
            "winner",
            "rank",
        ):
            offenders = [
                name for name in names if absent in str(name).lower()
            ]
            assert offenders == [], absent

    def test_a_synthesizer_must_implement_the_port(self) -> None:
        with pytest.raises(ValueError):
            ArchitectureSynthesis(
                FakeProposalRepository(),
                FakeAudit(),
                _Transactions(),
                synthesis=object(),
            )

    def test_the_collaborators_are_the_only_ports_it_holds(self) -> None:
        engine, _proposals, _audit = fake_synthesis()

        assert engine._proposals is not None
        assert engine._audit is not None
        assert engine._transactions is not None
        assert engine._synthesis is None
        assert engine.has_synthesizer is False

    def test_generating_a_proposal_touches_no_assistant_state(
        self, sqlite_storage
    ) -> None:
        before = assistant_snapshot(sqlite_storage)
        engine = build_synthesis(sqlite_storage)

        proposal = engine.synthesize(
            review_payload(), actor=ACTOR, reason=REASON
        )

        assert assistant_snapshot(sqlite_storage) == before
        assert proposal.project == PROJECT
        assert proposal.status is ProposalStatus.DRAFT

    def test_the_proposal_never_becomes_an_architecture_version(
        self, sqlite_storage
    ) -> None:
        engine = build_synthesis(sqlite_storage)
        before = len(sqlite_storage.architecture_versions.list())

        engine.synthesize(review_payload(), actor=ACTOR, reason=REASON)

        versions = sqlite_storage.architecture_versions.list()
        assert len(versions) == before
        assert not [item for item in versions if item.version == PROJECT]


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
    """Every string literal the module's *code* uses (docstrings excluded).

    Docstrings are prose: they legitimately name the concepts synthesis
    deliberately refuses (a vote, a ranking), so only real literals count.
    """
    tree = module_node()
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef)
        ):
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


def assistant_snapshot(storage: SqliteStorage) -> tuple[Any, ...]:
    """The assistant's own architecture: it must never move for a proposal."""
    return (
        tuple(item.to_dict() for item in storage.architecture_versions.list()),
        tuple(item.to_dict() for item in storage.change_requests.list()),
        tuple(item.to_dict() for item in storage.adrs.list()),
        tuple(item.to_dict() for item in storage.risks.list()),
    )


class TestPersistence:
    """A proposal outlives the process; the audit records exactly one write."""

    def test_a_generated_proposal_is_stored_and_readable(
        self, sqlite_storage
    ) -> None:
        engine = build_synthesis(sqlite_storage)

        proposal = engine.synthesize(
            review_payload(), actor=ACTOR, reason=REASON
        )

        stored = sqlite_storage.proposals.get(proposal.proposal_id)
        assert stored is not None
        assert stored == proposal
        assert stored.to_dict() == proposal.to_dict()

    def test_the_board_queries_the_repository(self, sqlite_storage) -> None:
        engine = build_synthesis(sqlite_storage)
        assert engine.proposals() == ()
        assert engine.latest(PROJECT) is None

        proposal = engine.synthesize(
            review_payload(), actor=ACTOR, reason=REASON
        )

        assert engine.proposals() == (proposal,)
        assert engine.latest(PROJECT) == proposal
        assert engine.require(proposal.proposal_id) == proposal

    def test_an_unknown_proposal_id_is_refused(self, sqlite_storage) -> None:
        from architecture_assistant.application import SynthesisNotFoundError

        engine = build_synthesis(sqlite_storage)

        with pytest.raises(SynthesisNotFoundError):
            engine.require("proposal-does-not-exist")

    def test_one_proposal_writes_exactly_one_audit_entry(
        self, sqlite_storage
    ) -> None:
        engine = build_synthesis(sqlite_storage)

        proposal = engine.synthesize(
            review_payload(), actor=ACTOR, reason=REASON
        )

        entries = [
            entry
            for entry in sqlite_storage.audit.list()
            if entry.entity_type is AuditEntityType.PROPOSAL
        ]
        assert len(entries) == 1
        entry = entries[0]
        assert entry.action is AuditAction.CREATE
        assert entry.entity_id == proposal.proposal_id
        assert entry.detail["operation"] == "generate-proposal"
        assert entry.detail["actor"] == ACTOR
        assert entry.detail["reason"] == REASON
        assert entry.detail["status"] == "DRAFT"
        assert entry.detail["fingerprint"] == proposal.fingerprint
        assert entry.detail["synthesis_source"] == DETERMINISTIC_SOURCE
        assert entry.detail["provider_count"] == 3
        assert entry.detail["finding_count"] == 1

    def test_a_reopened_database_still_holds_the_proposal(
        self, tmp_path
    ) -> None:
        path = str(tmp_path / "assistant.db")
        connection = open_database(path)
        try:
            engine = build_synthesis(SqliteStorage(connection))
            proposal = engine.synthesize(
                review_payload(), actor=ACTOR, reason=REASON
            )
        finally:
            close_database(connection)

        reopened = open_database(path)
        try:
            storage = SqliteStorage(reopened)
            stored = storage.proposals.get(proposal.proposal_id)
            assert stored is not None
            assert stored.proposal_id == proposal.proposal_id
            assert stored.status is ProposalStatus.DRAFT
            assert stored.review_digest["provider_count"] == 3
            assert stored.requirement == REQUIREMENT
            assert fingerprint_matches(stored) is True
            assert engine_like(storage).latest(PROJECT) == stored
        finally:
            close_database(reopened)


def engine_like(storage: SqliteStorage) -> ArchitectureSynthesis:
    """A synthesis over a reopened database - the same wiring, a new process."""
    return ArchitectureSynthesis(
        storage.proposals,
        storage.audit,
        storage,
        clock=fixed_clock(),
    )


class TestRevisionLineage:
    """A revision is explicit and additive: history is never rewritten."""

    def test_a_revision_is_a_distinct_proposal_with_lineage(
        self, sqlite_storage
    ) -> None:
        engine = build_synthesis(sqlite_storage)
        first = engine.synthesize(
            review_payload(), actor=ACTOR, reason=REASON
        )

        second = engine.synthesize(
            review_payload(),
            requirement="add a subtitle track?",
            revision_of=first.proposal_id,
            actor=ACTOR,
            reason="the operator asked for a revision",
        )

        assert second.proposal_id != first.proposal_id
        assert second.revision_no == 2
        assert second.revision_of == first.proposal_id
        assert second.requirement == "add a subtitle track?"
        # both rows stay: the first is superseded, never deleted or edited
        stored_first = sqlite_storage.proposals.get(first.proposal_id)
        assert stored_first is not None
        assert stored_first.status is ProposalStatus.SUPERSEDED
        assert stored_first.superseded_by == second.proposal_id
        assert len(engine.proposals()) == 2

    def test_a_revision_is_one_transaction_and_one_audit_entry(
        self, sqlite_storage
    ) -> None:
        engine = build_synthesis(sqlite_storage)
        first = engine.synthesize(
            review_payload(), actor=ACTOR, reason=REASON
        )

        engine.synthesize(
            review_payload(),
            revision_of=first.proposal_id,
            actor=ACTOR,
            reason="a revision",
        )

        entries = [
            entry
            for entry in sqlite_storage.audit.list()
            if entry.entity_type is AuditEntityType.PROPOSAL
        ]
        assert len(entries) == 2

    def test_a_decided_proposal_cannot_be_superseded(
        self, sqlite_storage
    ) -> None:
        from architecture_assistant.domain.models import utc_now

        engine = build_synthesis(sqlite_storage)
        first = engine.synthesize(
            review_payload(), actor=ACTOR, reason=REASON
        )
        decided = sqlite_storage.proposals.get(first.proposal_id)
        assert decided is not None
        sqlite_storage.proposals.upsert(
            replace(
                decided,
                status=ProposalStatus.APPROVED,
                decided_by=ACTOR,
                decided_at=utc_now(),
                decision_reason="accepted",
            )
        )

        with pytest.raises(SynthesisError):
            engine.synthesize(
                review_payload(),
                revision_of=first.proposal_id,
                actor=ACTOR,
                reason="a revision",
            )

        assert len(engine.proposals()) == 1

    def test_a_revision_cannot_cross_projects(self, sqlite_storage) -> None:
        engine = build_synthesis(sqlite_storage)
        first = engine.synthesize(
            review_payload(), actor=ACTOR, reason=REASON
        )

        with pytest.raises(SynthesisError):
            engine.synthesize(
                review_payload(project="another_project"),
                revision_of=first.proposal_id,
                actor=ACTOR,
                reason="a revision",
            )

        assert len(engine.proposals()) == 1

    def test_an_unknown_predecessor_is_refused(self, sqlite_storage) -> None:
        engine = build_synthesis(sqlite_storage)

        with pytest.raises(SynthesisError):
            engine.synthesize(
                review_payload(),
                revision_of="proposal-does-not-exist",
                actor=ACTOR,
                reason="a revision",
            )

        assert engine.proposals() == ()
