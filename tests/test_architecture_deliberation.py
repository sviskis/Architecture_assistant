"""Tests for the Step 29 controlled architecture deliberation.

This suite pins the *properties of the protocol*, not the wording of a prompt. A
deliberation is a controlled two-round review board, and the properties that make
it controlled are exactly what is asserted here:

* **Round-1 independence is structural.** Both architects receive the identical
  operator requirement and the same bounded context, and the Round-1 request has
  no field that could carry a peer's proposal at all;
* **the chair is not a judge and not a supervisor.** It reviews two structured
  proposals and synthesizes a design; it cannot vote, cannot rank a provider,
  cannot approve anything and cannot reach ``VERIFIED``;
* **exactly one review round.** ``max_review_rounds`` is one and the run has no
  transition back to ``ROUND2_RUNNING``, so no debate loop exists;
* **a disagreement the answers prove is never averaged away.** The remaining
  disagreements and the advisor-change summary are computed from the Round-2
  answers and written *over* whatever the chair claimed;
* **fail-closed and honest.** A disabled seat abstains with no call, a provider
  failure preserves every completed stage, a one-architect board is recorded
  ``INSUFFICIENT_PEER_REVIEW``, and a malformed answer is a failure - never a
  design;
* **advisory only.** The proposal the deliberation generates is a ``DRAFT`` until
  a human decides through the unchanged ``ProposalApproval`` path, and a stale
  synthesis can never silently produce one.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from architecture_assistant.application import (
    ACTION_CANCEL,
    ACTION_GENERATE_FINAL_SYNTHESIS,
    ACTION_GENERATE_LEAD_REVIEW,
    ACTION_GENERATE_PROPOSAL,
    ACTION_RUN_ROUND1,
    ACTION_RUN_ROUND2,
    ArchitectureDeliberation,
    DeliberationActorRequiredError,
    DeliberationChair,
    DeliberationError,
    DeliberationNotFoundError,
    DeliberationReasonRequiredError,
    DeliberationRequirementRequiredError,
    DeliberationResponseError,
    DeliberationSeat,
    DeliberationStageOrderError,
    DeliberationStaleError,
    fingerprint_matches,
    peer_claims,
)
from architecture_assistant.domain.audit import AuditEntityType
from architecture_assistant.domain.enums import (
    DELIBERATION_MAX_REVIEW_ROUNDS,
    DeliberationStage,
    DeliberationStageStatus,
    DeliberationStatus,
    ProposalStatus,
)
from architecture_assistant.domain.models import (
    ArchitectureProposal,
    DeliberationArtifact,
    DeliberationRun,
    utc_now,
)
from architecture_assistant.ports.capabilities import (
    SLOT_AGENT_A,
    SLOT_AGENT_B,
    SLOT_LEAD,
    AgentAnalysisResult,
    AgentReconsiderResult,
    FinalSynthesisResult,
    LeadReviewResult,
)

REQUIREMENT = (
    "Man ir MP3 fails un vajag TXT transkripciju. "
    "Gribu vienkarsu Windows Python programmu ar GUI."
)
ACTOR = "operator"
REASON = "step 29 test"


def fixed_clock():
    """A monotonic fake clock, so nothing depends on the wall clock."""
    counter = {"n": 0}

    def clock() -> datetime:
        counter["n"] += 1
        return datetime(2026, 9, 22, 12, 0, counter["n"], tzinfo=timezone.utc)

    return clock


# ---------------------------------------------------------------------------
# scripted doubles
# ---------------------------------------------------------------------------

class ScriptedAgent:
    """An architect that answers from a script and records every request."""

    def __init__(
        self,
        name: str,
        summary: str,  # noqa: A002 - the doubled name reads better here
        *,
        decision: str = "KEEP",
        fail_analyse: bool = False,
        fail_reconsider: bool = False,
        analyse_raw=None,
        reconsider_raw=None,
        cost=None,
    ) -> None:
        self.name = name
        self.script_summary = summary
        self.decision = decision
        self.fail_analyse = fail_analyse
        self.fail_reconsider = fail_reconsider
        self.analyse_raw = analyse_raw
        self.reconsider_raw = reconsider_raw
        self.cost = (
            cost if cost is not None else {"available": True, "total_usd": 0.01}
        )
        self.analyses: list = []
        self.reconsiderations: list = []

    @property
    def analyse_calls(self) -> int:
        return len(self.analyses)

    @property
    def reconsider_calls(self) -> int:
        return len(self.reconsiderations)

    def analyse(self, query):
        self.analyses.append(query)
        if self.fail_analyse:
            raise RuntimeError("provider exploded")
        raw = self.analyse_raw
        if raw is None:
            raw = {
                "proposal_summary": self.script_summary,
                "modules": [{"name": f"module-{self.name}"}],
                "dependencies": [{"from": "a", "to": "b"}],
                "risks": [{"title": f"risk-{self.name}"}],
                "assumptions": [f"assumption-{self.name}"],
                "open_questions": [f"question-{self.name}"],
                "confidence": 0.7,
            }
        return AgentAnalysisResult(source=self.name, content=raw, cost=self.cost)

    def reconsider(self, query):
        self.reconsiderations.append(query)
        if self.fail_reconsider:
            raise RuntimeError("provider exploded")
        raw = self.reconsider_raw
        if raw is None:
            raw = {
                "decision": self.decision,
                "revised_summary": f"{self.script_summary} reconsidered",
                "changed_decisions": (
                    [{"what": "changed"}] if self.decision == "REVISE" else []
                ),
                "unchanged_decisions": [{"what": "kept"}],
                "remaining_disagreements": [f"disagreement-{self.name}"],
                "new_risks": [{"title": f"new-risk-{self.name}"}],
                "remaining_questions": [f"remaining-{self.name}"],
            }
        return AgentReconsiderResult(source=self.name, content=raw, cost=self.cost)


class ScriptedLead:
    """The chair: a review answer, a synthesis answer and a call log."""

    def __init__(
        self,
        *,
        fail_review: bool = False,
        fail_synthesis: bool = False,
        review_raw=None,
        synthesis_raw=None,
        claims_consensus: bool = False,
        cost=None,
    ) -> None:
        self.fail_review = fail_review
        self.fail_synthesis = fail_synthesis
        self.review_raw = review_raw
        self.synthesis_raw = synthesis_raw
        self.claims_consensus = claims_consensus
        self.cost = (
            cost if cost is not None else {"available": True, "total_usd": 0.05}
        )
        self.reviews: list = []
        self.syntheses: list = []

    def review(self, query):
        self.reviews.append(query)
        if self.fail_review:
            raise RuntimeError("provider exploded")
        raw = self.review_raw
        if raw is None:
            raw = {
                "agreements": ["both propose a CLI core"],
                "conflicts": ["local engine vs remote API"],
                "weak_assumptions": ["both assume ffmpeg is present"],
                "missing_information": ["target Windows version"],
                "risk_deltas": ["A adds a deployment risk"],
                "questions_for_agent_a": ["which assumption is weakest?"],
                "questions_for_agent_b": ["is the other approach stronger?"],
                "common_questions": ["must the GUI be optional?"],
                "recommendations_to_reconsider": ["state the boundary"],
                "unresolved_questions": ["which audio codec?"],
            }
        return LeadReviewResult(source="openai", content=raw, cost=self.cost)

    def synthesize(self, query):
        self.syntheses.append(query)
        if self.fail_synthesis:
            raise RuntimeError("provider exploded")
        raw = self.synthesis_raw
        if raw is None:
            raw = {
                "summary": "A Windows Python GUI over a reusable CLI core.",
                "modules": [{"name": "core", "responsibility": "pipeline"}],
                "responsibilities": ["transcription"],
                "dependencies": [{"from": "gui", "to": "core"}],
                "boundaries": ["the engine is behind a port"],
                "data_flows": [{"from": "mp3", "to": "txt"}],
                "external_dependencies": [{"name": "ffmpeg"}],
                "architecture_decisions": [{"id": "AD-1"}],
                "accepted_points": ["both want a CLI core"],
                "rejected_alternatives": [{"alternative": "monolith"}],
                "remaining_disagreements": (
                    [] if self.claims_consensus else ["which codec?"]
                ),
                "risks": [{"title": "codec availability"}],
                "adr_candidates": [{"title": "Transcription port"}],
                "implementation_phases": [{"phase": 1, "goal": "core"}],
                "unresolved_questions": ["which codec?"],
                "rationale": "chosen for maintainability",
            }
        return FinalSynthesisResult(source="openai", content=raw, cost=self.cost)


# ---------------------------------------------------------------------------
# in-memory collaborators (the exact repository ports)
# ---------------------------------------------------------------------------

class FakeDeliberations:
    """A deliberation repository with the port's exact contract."""

    def __init__(self, runs=(), artifacts=()) -> None:
        self.runs = {run.deliberation_id: run for run in runs}
        self.artifacts = list(artifacts)
        self.run_writes = 0

    def upsert_run(self, run: DeliberationRun) -> None:
        self.run_writes += 1
        self.runs[run.deliberation_id] = run

    def get_run(self, deliberation_id: str):
        return self.runs.get(deliberation_id)

    def list_runs(self):
        return tuple(self.runs.values())

    def list_runs_by_status(self, status):
        return tuple(run for run in self.runs.values() if run.status == status)

    def list_runs_for_project(self, project: str):
        return tuple(run for run in self.runs.values() if run.project == project)

    def upsert_artifact(self, artifact: DeliberationArtifact) -> None:
        self.artifacts = [
            item
            for item in self.artifacts
            if item.artifact_id != artifact.artifact_id
        ] + [artifact]

    def get_artifact(self, artifact_id: str):
        for item in self.artifacts:
            if item.artifact_id == artifact_id:
                return item
        return None

    def list_artifacts(self, deliberation_id: str):
        return tuple(
            item
            for item in self.artifacts
            if item.deliberation_id == deliberation_id
        )

    def list_artifacts_for_stage(self, deliberation_id, stage, slot=""):
        return tuple(
            item
            for item in self.artifacts
            if item.deliberation_id == deliberation_id
            and item.stage == stage
            and (not slot or item.slot == slot)
        )

    def delete(self, deliberation_id: str) -> bool:
        self.artifacts = [
            item
            for item in self.artifacts
            if item.deliberation_id != deliberation_id
        ]
        return self.runs.pop(deliberation_id, None) is not None


class FakeProposals:
    """A proposal repository with the port's exact contract."""

    def __init__(self, items=()) -> None:
        self.items = {item.proposal_id: item for item in items}

    def upsert(self, proposal: ArchitectureProposal) -> None:
        self.items[proposal.proposal_id] = proposal

    def get(self, proposal_id: str):
        return self.items.get(proposal_id)

    def list(self):
        return tuple(self.items.values())

    def list_by_status(self, status):
        return tuple(item for item in self.items.values() if item.status == status)

    def list_for_project(self, project: str):
        return tuple(
            item for item in self.items.values() if item.project == project
        )

    def delete(self, proposal_id: str) -> bool:
        return self.items.pop(proposal_id, None) is not None


class FakeProject:
    """The single managed project, as the project repository reports it."""

    def __init__(self, name: str = "youtube_to_mp3") -> None:
        self.name = name


class FakeProjects:
    """A project repository that reports exactly one managed project."""

    def __init__(self, name: str = "youtube_to_mp3") -> None:
        self.items = (FakeProject(name),)

    def list(self):
        return self.items

    def get(self, name: str):
        return self.items[0] if name == self.items[0].name else None

    def upsert(self, project) -> None:  # pragma: no cover - never called
        raise AssertionError("the deliberation must never write a project")

    def delete(self, name: str) -> bool:  # pragma: no cover - never called
        raise AssertionError("the deliberation must never delete a project")


class FakeAudit:
    """An append-only audit trail, kept in memory."""

    def __init__(self) -> None:
        self.entries: list = []

    def append(self, entry) -> None:
        self.entries.append(entry)

    def list(self):
        return tuple(self.entries)

    def list_for_entity(self, entity_type, entity_id: str):
        return tuple(
            entry
            for entry in self.entries
            if entry.entity_type == entity_type and entry.entity_id == entity_id
        )


class FakeTransactions:
    """A transaction boundary that counts its openings."""

    def __init__(self) -> None:
        self.opened = 0

    @contextmanager
    def transaction(self):
        self.opened += 1
        yield None


class _Unset:
    """A sentinel: ``None`` is a *meaningful* value for a seat implementation."""


_UNSET = _Unset()


class Harness:
    """One wired deliberation plus every double behind it."""

    def __init__(
        self,
        *,
        agent_a=None,
        agent_b=None,
        lead=None,
        project: str = "youtube_to_mp3",
        clock=None,
        agent_a_seat: tuple[str, str] = ("deepseek", "deepseek-chat"),
        agent_b_seat: tuple[str, str] = ("claude", "claude-x"),
        chair: tuple[str, str] = ("openai", "gpt-5.6"),
        agent_a_seat_port=_UNSET,
        agent_b_seat_port=_UNSET,
        lead_port=_UNSET,
    ) -> None:
        self.agent_a = agent_a or ScriptedAgent("deepseek", "A plan")
        self.agent_b = agent_b or ScriptedAgent("claude", "B plan")
        self.lead = lead or ScriptedLead()
        self.deliberations = FakeDeliberations()
        self.proposals = FakeProposals()
        self.projects = FakeProjects(project)
        self.audit = FakeAudit()
        self.transactions = FakeTransactions()
        self.events: list = []
        self.engine = ArchitectureDeliberation(
            self.deliberations,
            self.proposals,
            self.projects,
            self.audit,
            self.transactions,
            agent_a=DeliberationSeat(
                SLOT_AGENT_A,
                *agent_a_seat,
                (
                    self.agent_a
                    if isinstance(agent_a_seat_port, _Unset)
                    else agent_a_seat_port
                ),
            ),
            agent_b=DeliberationSeat(
                SLOT_AGENT_B,
                *agent_b_seat,
                (
                    self.agent_b
                    if isinstance(agent_b_seat_port, _Unset)
                    else agent_b_seat_port
                ),
            ),
            chair=DeliberationChair(
                *chair,
                (self.lead if isinstance(lead_port, _Unset) else lead_port),
            ),
            clock=clock or fixed_clock(),
        )

    @property
    def sink(self):
        """The event sink this harness records into."""
        return self.events.append

    def start(self, requirement: str = REQUIREMENT, **kwargs) -> DeliberationRun:
        """Start a run on this harness."""
        return self.engine.start(
            requirement,
            actor=ACTOR,
            reason=REASON,
            on_event=self.sink,
            **kwargs,
        )

    def round1(self, run: DeliberationRun) -> DeliberationRun:
        return self.engine.run_round1(
            run.deliberation_id, actor=ACTOR, reason=REASON, on_event=self.sink
        )

    def lead_review(self, run: DeliberationRun) -> DeliberationRun:
        return self.engine.generate_lead_review(
            run.deliberation_id, actor=ACTOR, reason=REASON, on_event=self.sink
        )

    def round2(self, run: DeliberationRun) -> DeliberationRun:
        return self.engine.run_round2(
            run.deliberation_id, actor=ACTOR, reason=REASON, on_event=self.sink
        )

    def synthesis(self, run: DeliberationRun) -> DeliberationRun:
        return self.engine.generate_final_synthesis(
            run.deliberation_id, actor=ACTOR, reason=REASON, on_event=self.sink
        )

    def proposal(self, run: DeliberationRun, **kwargs) -> ArchitectureProposal:
        return self.engine.generate_proposal(
            run.deliberation_id,
            actor=ACTOR,
            reason=REASON,
            on_event=self.sink,
            **kwargs,
        )

    def full(self, requirement: str = REQUIREMENT) -> DeliberationRun:
        """Drive the whole protocol: R1 -> review -> R2 -> synthesis."""
        run = self.start(requirement)
        run = self.round1(run)
        run = self.lead_review(run)
        run = self.round2(run)
        return self.synthesis(run)

    def artifact(self, deliberation_id: str, stage, slot: str = ""):
        """The newest artifact of one stage of one run."""
        return self.engine.latest_artifact(deliberation_id, stage, slot)

    def event_actions(self) -> list[str]:
        """The action name of every emitted event, in order."""
        return [event["action"] for event in self.events]

    def actions(self, deliberation_id: str) -> tuple[str, ...]:
        """The actions the run currently offers."""
        return tuple(
            self.engine.snapshot(deliberation_id).available_actions
        )


@pytest.fixture
def harness() -> Harness:
    """A freshly wired deliberation over scripted doubles."""
    return Harness()


# ---------------------------------------------------------------------------
# start: identity, lineage, validation
# ---------------------------------------------------------------------------

class TestStart:
    """One requirement, one identity, one DRAFT run."""

    def test_start_creates_a_draft_run_with_the_three_seats(
        self, harness: Harness
    ) -> None:
        run = harness.start()

        assert run.status is DeliberationStatus.DRAFT
        assert run.project == "youtube_to_mp3"
        assert run.requirement == REQUIREMENT
        assert run.agent_a_provider == "deepseek"
        assert run.agent_b_provider == "claude"
        assert run.lead_provider == "openai"
        assert run.max_review_rounds == DELIBERATION_MAX_REVIEW_ROUNDS == 1
        assert len(harness.audit.entries) == 1
        assert harness.audit.entries[0].entity_type is AuditEntityType.DELIBERATION
        assert harness.audit.entries[0].detail["operation"] == "create-deliberation"

    def test_the_requirement_is_stored_verbatim(self, harness: Harness) -> None:
        run = harness.start()

        assert run.requirement == REQUIREMENT
        stored = harness.deliberations.get_run(run.deliberation_id)
        assert stored.requirement == REQUIREMENT

    def test_the_same_requirement_yields_the_same_identity(self) -> None:
        first = Harness().start()
        second = Harness().start()

        assert first.deliberation_id == second.deliberation_id
        assert first.fingerprint == second.fingerprint

    def test_starting_twice_is_idempotent(self, harness: Harness) -> None:
        first = harness.start()
        second = harness.start()

        assert first.deliberation_id == second.deliberation_id
        assert len(harness.deliberations.runs) == 1
        assert len(harness.audit.entries) == 1

    def test_a_changed_requirement_is_a_different_deliberation(self) -> None:
        first = Harness().start()
        second = Harness().start(REQUIREMENT + " Un vajag ari testus.")

        assert first.deliberation_id != second.deliberation_id
        assert first.fingerprint != second.fingerprint

    def test_changed_providers_are_a_different_deliberation(self) -> None:
        first = Harness().start()
        second = Harness(
            agent_a=ScriptedAgent("grok", "B"),
            agent_a_seat=("grok", "grok-4.6"),
        ).start()

        assert first.deliberation_id != second.deliberation_id

    def test_changed_context_is_a_different_deliberation(self) -> None:
        first = Harness().start(context={"baseline": "1.1"})
        second = Harness().start(context={"baseline": "1.2"})

        assert first.deliberation_id != second.deliberation_id

    def test_a_blank_requirement_is_refused(self, harness: Harness) -> None:
        with pytest.raises(DeliberationRequirementRequiredError):
            harness.start("   ")

    def test_an_explicit_actor_and_reason_are_required(
        self, harness: Harness
    ) -> None:
        with pytest.raises(DeliberationActorRequiredError):
            harness.engine.start(REQUIREMENT, actor=" ", reason=REASON)
        with pytest.raises(DeliberationReasonRequiredError):
            harness.engine.start(REQUIREMENT, actor=ACTOR, reason=" ")

    def test_the_run_is_audited_once_and_never_as_prose(
        self, harness: Harness
    ) -> None:
        run = harness.start()
        entries = harness.audit.list_for_entity(
            AuditEntityType.DELIBERATION, run.deliberation_id
        )

        assert len(entries) == 1
        detail = entries[0].detail
        assert detail["actor"] == ACTOR
        assert detail["reason"] == REASON
        assert detail["project"] == "youtube_to_mp3"
        assert detail["agent_a"] == "deepseek:deepseek-chat"
        assert detail["lead"] == "openai:gpt-5.6"


class TestNoWorkflowAuthority:
    """The deliberation is advisory: it decides and approves nothing."""

    def test_the_use_case_exposes_no_authority_verb(self) -> None:
        public = {
            name
            for name in dir(ArchitectureDeliberation)
            if not name.startswith("_")
        }

        for forbidden in (
            "approve",
            "reject",
            "verify",
            "dispatch",
            "monitor",
            "orchestrator",
            "realization",
            "evolution",
            "versioning",
        ):
            assert not any(forbidden in name for name in public), forbidden

    def test_the_use_case_module_never_names_workflow_or_infrastructure(
        self,
    ) -> None:
        import ast
        import pathlib

        import architecture_assistant.application as package

        path = (
            pathlib.Path(package.__file__).parent / "architecture_deliberation.py"
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Only *identifiers* are inspected - never string literals - because the
        # module's own docstrings legitimately name what it is NOT handed.
        named: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                named.add(node.id)
            elif isinstance(node, ast.Attribute):
                named.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                named.add(node.module or "")
                named.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                named.update(alias.name for alias in node.names)

        for forbidden in (
            "ArchitectureEvolution",
            "ArchitectureVersioning",
            "ADRManager",
            "RiskManager",
            "RealizationControl",
            "Orchestrator",
            "Scheduler",
            "Monitor",
            "StepState",
            "SupervisorPort",
            "JudgeUseCase",
            "sqlite3",
        ):
            assert forbidden not in named, forbidden
        assert not any(
            name.startswith("infrastructure") for name in named
        ), "the application layer never imports infrastructure"

    def test_the_run_reaches_ready_for_proposal_and_never_verified(
        self, harness: Harness
    ) -> None:
        run = harness.full()

        assert run.status is DeliberationStatus.READY_FOR_PROPOSAL
        assert run.status.value != "VERIFIED"


# ---------------------------------------------------------------------------
# round 1: structural independence
# ---------------------------------------------------------------------------

class TestRound1Independence:
    """Both architects answer the same requirement and cannot see each other."""

    def test_both_architects_receive_the_identical_requirement(
        self, harness: Harness
    ) -> None:
        harness.round1(harness.start())

        query_a = harness.agent_a.analyses[0]
        query_b = harness.agent_b.analyses[0]

        assert query_a.requirement == query_b.requirement == REQUIREMENT

    def test_both_architects_receive_the_same_bounded_context(
        self, harness: Harness
    ) -> None:
        context = {"baseline": "1.1", "open_risks": ["R-010"]}
        run = harness.start(context=context)
        harness.engine.run_round1(
            run.deliberation_id,
            actor=ACTOR,
            reason=REASON,
            context=context,
            on_event=harness.sink,
        )

        query_a = harness.agent_a.analyses[0]
        query_b = harness.agent_b.analyses[0]

        assert dict(query_a.context) == dict(query_b.context) == context

    def test_the_round_1_request_cannot_carry_a_peer_answer(
        self, harness: Harness
    ) -> None:
        harness.round1(harness.start())
        query_a = harness.agent_a.analyses[0]

        for forbidden in (
            "peer_claims",
            "peer",
            "other_agent",
            "lead_critique",
            "own_round1",
        ):
            assert not hasattr(query_a, forbidden), forbidden

    def test_agent_b_is_never_asked_before_agent_a_answered(
        self, harness: Harness
    ) -> None:
        order: list[str] = []
        original_a = harness.agent_a.analyse
        original_b = harness.agent_b.analyse

        def analyse_a(query):
            order.append("agent_a")
            return original_a(query)

        def analyse_b(query):
            order.append("agent_b")
            return original_b(query)

        harness.agent_a.analyse = analyse_a
        harness.agent_b.analyse = analyse_b
        harness.round1(harness.start())

        assert order == ["agent_a", "agent_b"]

    def test_round_1_persists_one_addressable_artifact_per_seat(
        self, harness: Harness
    ) -> None:
        run = harness.round1(harness.start())
        artifacts = harness.engine.artifacts(run.deliberation_id)

        assert len(artifacts) == 2
        assert {artifact.stage for artifact in artifacts} == {
            DeliberationStage.AGENT_A_ROUND1,
            DeliberationStage.AGENT_B_ROUND1,
        }
        assert all(
            artifact.status is DeliberationStageStatus.COMPLETE
            for artifact in artifacts
        )

    def test_the_round_1_result_preserves_the_structured_proposal(
        self, harness: Harness
    ) -> None:
        run = harness.round1(harness.start())
        content = harness.artifact(
            run.deliberation_id, DeliberationStage.AGENT_A_ROUND1, SLOT_AGENT_A
        ).content

        for key in (
            "advisor_id",
            "provider",
            "model",
            "requirement",
            "proposal_summary",
            "modules",
            "dependencies",
            "data_flows",
            "external_dependencies",
            "architecture_choices",
            "risks",
            "assumptions",
            "open_questions",
            "alternatives",
            "recommended_decisions",
            "evidence",
            "confidence",
        ):
            assert key in content, key
        assert content["provider"] == "deepseek"
        assert content["requirement"] == REQUIREMENT
        assert content["round_no"] == 1
        assert content["confidence"] == 0.7
        # a section the provider omitted is an explicit empty list, not invented
        assert content["data_flows"] == []

    def test_the_round_1_sequence_carries_the_lifecycle_events(
        self, harness: Harness
    ) -> None:
        harness.round1(harness.start())

        assert harness.event_actions() == [
            "deliberation-created",
            "round1-started",
            "agent-a-round1-started",
            "agent-a-round1-completed",
            "agent-b-round1-started",
            "agent-b-round1-completed",
            "round1-completed",
        ]


# ---------------------------------------------------------------------------
# the chair's review
# ---------------------------------------------------------------------------

class TestLeadReview:
    """The chair receives both Round-1 results and produces criticism, not design."""

    def test_the_chair_receives_both_structured_round_1_results(
        self, harness: Harness
    ) -> None:
        run = harness.start()
        run = harness.round1(run)
        harness.lead_review(run)

        query = harness.lead.reviews[0]

        assert query.requirement == REQUIREMENT
        assert query.agent_a["proposal_summary"] == "A plan"
        assert query.agent_b["proposal_summary"] == "B plan"
        assert query.agent_a["provider"] == "deepseek"
        assert query.agent_b["provider"] == "claude"

    def test_the_chair_is_not_handed_a_transcript_or_a_credential(
        self, harness: Harness
    ) -> None:
        run = harness.start()
        run = harness.round1(run)
        harness.lead_review(run)

        serialized = json.dumps(harness.lead.reviews[0].to_dict(), sort_keys=True)

        assert "api_key" not in serialized
        assert "authorization" not in serialized
        assert "chain_of_thought" not in serialized

    def test_the_review_is_not_a_final_synthesis(self, harness: Harness) -> None:
        run = harness.full()
        review = harness.artifact(
            run.deliberation_id, DeliberationStage.LEAD_REVIEW, SLOT_LEAD
        )
        synthesis = harness.artifact(
            run.deliberation_id, DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
        )

        assert review.stage is not synthesis.stage
        assert review.artifact_id != synthesis.artifact_id
        assert "agreements" in review.content
        assert "summary" not in review.content
        assert "modules" not in review.content
        assert synthesis.content["summary"]

    def test_every_review_section_is_present(self, harness: Harness) -> None:
        run = harness.full()
        content = harness.artifact(
            run.deliberation_id, DeliberationStage.LEAD_REVIEW, SLOT_LEAD
        ).content

        for key in (
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
        ):
            assert key in content, key
        assert content["agreements"] == ["both propose a CLI core"]
        assert content["questions_for_agent_a"] == ["which assumption is weakest?"]

    def test_one_usable_architect_yields_insufficient_peer_review(self) -> None:
        harness = Harness(agent_b=ScriptedAgent("claude", "B", fail_analyse=True))
        run = harness.round1(harness.start())
        assert run.status is DeliberationStatus.ROUND1_COMPLETE

        run = harness.lead_review(run)

        assert run.status is DeliberationStatus.ERROR
        assert "insufficient peer review" in run.error_reason
        review = harness.artifact(
            run.deliberation_id, DeliberationStage.LEAD_REVIEW, SLOT_LEAD
        )
        assert review.status is DeliberationStageStatus.INSUFFICIENT_PEER_REVIEW
        # the chair was never asked: a one-architect board is not a deliberation
        assert harness.lead.reviews == []

    def test_a_run_that_stopped_short_never_reaches_a_synthesis(self) -> None:
        harness = Harness(agent_b=ScriptedAgent("claude", "B", fail_analyse=True))
        run = harness.round1(harness.start())
        run = harness.lead_review(run)

        with pytest.raises(DeliberationStageOrderError):
            harness.round2(run)

    def test_a_failing_chair_preserves_the_advisor_results(self) -> None:
        harness = Harness(lead=ScriptedLead(fail_review=True))
        run = harness.round1(harness.start())
        run = harness.lead_review(run)

        assert run.status is DeliberationStatus.ERROR
        assert run.error_reason.startswith("lead provider failed")
        artifacts = harness.engine.artifacts(run.deliberation_id)
        assert len(artifacts) == 3
        assert sum(1 for item in artifacts if item.is_usable) == 2

    def test_a_malformed_chair_answer_fails_closed(self) -> None:
        harness = Harness(
            lead=ScriptedLead(review_raw={"agreements": "not a list"})
        )
        run = harness.round1(harness.start())
        run = harness.lead_review(run)

        assert run.status is DeliberationStatus.ERROR
        assert "malformed answer" in run.error_reason

    def test_a_chair_without_a_provider_fails_closed_without_a_call(self) -> None:
        harness = Harness(chair=("disabled", ""), lead_port=None)
        run = harness.round1(harness.start())
        run = harness.lead_review(run)

        assert run.status is DeliberationStatus.ERROR
        assert "no call was made" in run.error_reason
        assert harness.lead.reviews == []


# ---------------------------------------------------------------------------
# the review packet and round 2
# ---------------------------------------------------------------------------

class TestRound2:
    """Each architect reconsiders its own answer - exactly once."""

    def test_each_architect_receives_its_own_round_1_and_the_chair_critique(
        self, harness: Harness
    ) -> None:
        harness.full()

        query_a = harness.agent_a.reconsiderations[0]
        query_b = harness.agent_b.reconsiderations[0]

        assert query_a.own_round1["proposal_summary"] == "A plan"
        assert query_b.own_round1["proposal_summary"] == "B plan"
        assert query_a.own_round1["slot"] == SLOT_AGENT_A
        assert query_b.own_round1["slot"] == SLOT_AGENT_B
        assert query_a.lead_critique["questions"] == [
            "which assumption is weakest?"
        ]
        assert query_b.lead_critique["questions"] == [
            "is the other approach stronger?"
        ]
        assert query_a.lead_critique["conflicts"] == ["local engine vs remote API"]

    def test_the_packet_carries_only_structured_peer_claims(
        self, harness: Harness
    ) -> None:
        run = harness.full()

        peer_of_a = harness.agent_a.reconsiderations[0].peer_claims
        round1_b = harness.artifact(
            run.deliberation_id, DeliberationStage.AGENT_B_ROUND1, SLOT_AGENT_B
        ).content

        assert peer_of_a["proposal_summary"] == "B plan"
        assert peer_of_a == peer_claims(round1_b)
        # the peer's own identity, cost and lifecycle fields are not projected
        for withheld in ("advisor_id", "cost", "schema_version", "review_id"):
            assert withheld not in peer_of_a, withheld

    def test_the_packet_names_its_own_round_1_artifact(
        self, harness: Harness
    ) -> None:
        run = harness.full()
        round2 = harness.artifact(
            run.deliberation_id, DeliberationStage.AGENT_A_ROUND2, SLOT_AGENT_A
        )
        round1 = harness.artifact(
            run.deliberation_id, DeliberationStage.AGENT_A_ROUND1, SLOT_AGENT_A
        )

        assert round2.content["links_to_round1_id"] == round1.artifact_id

    @pytest.mark.parametrize(
        "decision, changed",
        [
            ("KEEP", []),
            ("REVISE", [{"what": "changed"}]),
            ("WITHDRAW", []),
        ],
    )
    def test_keep_revise_and_withdraw_are_each_supported(
        self, decision: str, changed: list
    ) -> None:
        agent = ScriptedAgent("deepseek", "A", decision=decision)
        agent.reconsider_raw = {
            "decision": decision,
            "revised_summary": "revised",
            "changed_decisions": changed,
            "remaining_disagreements": ["still open"],
        }
        harness = Harness(agent_a=agent)
        run = harness.full()
        round2 = harness.artifact(
            run.deliberation_id, DeliberationStage.AGENT_A_ROUND2, SLOT_AGENT_A
        )

        assert round2.status is DeliberationStageStatus.COMPLETE
        assert round2.content["decision"] == decision
        assert round2.content["changed_decisions"] == changed

    def test_a_keep_that_changed_decisions_is_refused(self) -> None:
        agent = ScriptedAgent("deepseek", "A", decision="KEEP")
        agent.reconsider_raw = {
            "decision": "KEEP",
            "changed_decisions": [{"what": "contradiction"}],
        }
        harness = Harness(agent_a=agent)
        run = harness.round1(harness.start())
        run = harness.lead_review(run)
        run = harness.round2(run)

        round2 = harness.artifact(
            run.deliberation_id, DeliberationStage.AGENT_A_ROUND2, SLOT_AGENT_A
        )
        assert round2.status is DeliberationStageStatus.ERROR
        assert "malformed answer" in round2.content["reason"]

    def test_an_unknown_decision_is_refused(self) -> None:
        agent = ScriptedAgent("deepseek", "A")
        agent.reconsider_raw = {"decision": "MAYBE"}
        harness = Harness(agent_a=agent)
        run = harness.round1(harness.start())
        run = harness.lead_review(run)
        run = harness.round2(run)

        round2 = harness.artifact(
            run.deliberation_id, DeliberationStage.AGENT_A_ROUND2, SLOT_AGENT_A
        )
        assert round2.status is DeliberationStageStatus.ERROR

    def test_round_2_runs_exactly_once(self, harness: Harness) -> None:
        run = harness.round1(harness.start())
        run = harness.lead_review(run)
        run = harness.round2(run)

        assert harness.agent_a.reconsider_calls == 1
        with pytest.raises(DeliberationStageOrderError):
            harness.round2(run)
        assert harness.agent_a.reconsider_calls == 1

    def test_there_is_no_debate_loop(self, harness: Harness) -> None:
        run = harness.full()

        assert harness.agent_a.analyse_calls == 1
        assert harness.agent_b.analyse_calls == 1
        assert harness.agent_a.reconsider_calls == 1
        assert harness.agent_b.reconsider_calls == 1
        assert run.max_review_rounds == 1

    def test_round_2_cannot_run_without_the_review(self, harness: Harness) -> None:
        run = harness.round1(harness.start())

        with pytest.raises(DeliberationStageOrderError):
            harness.round2(run)
        assert harness.agent_a.reconsider_calls == 0


# ---------------------------------------------------------------------------
# the final synthesis
# ---------------------------------------------------------------------------

class TestFinalSynthesis:
    """The chair synthesizes the exact upstream answers, and cannot fake consensus."""

    def test_the_chair_receives_the_exact_five_upstream_results(
        self, harness: Harness
    ) -> None:
        harness.full()
        query = harness.lead.syntheses[0]

        assert query.agent_a_round1["proposal_summary"] == "A plan"
        assert query.agent_b_round1["proposal_summary"] == "B plan"
        assert query.lead_review["agreements"] == ["both propose a CLI core"]
        assert query.agent_a_round2["decision"] == "KEEP"
        assert query.agent_b_round2["decision"] == "KEEP"

    def test_the_synthesis_preserves_every_required_section(
        self, harness: Harness
    ) -> None:
        run = harness.full()
        content = harness.artifact(
            run.deliberation_id, DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
        ).content

        for key in (
            "summary",
            "modules",
            "responsibilities",
            "dependencies",
            "boundaries",
            "data_flows",
            "external_dependencies",
            "architecture_decisions",
            "accepted_points",
            "rejected_alternatives",
            "remaining_disagreements",
            "risks",
            "adr_candidates",
            "implementation_phases",
            "unresolved_questions",
            "rationale",
            "advisor_change_summary",
        ):
            assert key in content, key

    def test_a_proved_disagreement_survives_a_chair_that_claims_consensus(
        self,
    ) -> None:
        harness = Harness(lead=ScriptedLead(claims_consensus=True))
        run = harness.full()
        content = harness.artifact(
            run.deliberation_id, DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
        ).content

        # the chair said "no disagreement"; the answers prove otherwise
        assert "disagreement-deepseek" in content["remaining_disagreements"]
        assert "disagreement-claude" in content["remaining_disagreements"]
        assert content["consensus"] is False

    def test_the_advisor_change_summary_is_the_seats_own_record(
        self, harness: Harness
    ) -> None:
        harness.full()
        content = harness.artifact(
            (harness.engine.runs())[0].deliberation_id,
            DeliberationStage.FINAL_SYNTHESIS,
            SLOT_LEAD,
        ).content

        summary = content["advisor_change_summary"]
        assert summary["decisions"] == {
            SLOT_AGENT_A: "KEEP",
            SLOT_AGENT_B: "KEEP",
        }

    def test_a_withdrawn_proposal_is_carried_as_a_disagreement(self) -> None:
        harness = Harness(agent_b=ScriptedAgent("claude", "B", decision="WITHDRAW"))
        run = harness.full()
        content = harness.artifact(
            run.deliberation_id, DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
        ).content

        assert any(
            "withdrew" in item for item in content["remaining_disagreements"]
        )
        assert content["consensus"] is False

    def test_the_synthesis_identity_depends_on_the_exact_upstream_artifacts(
        self, harness: Harness
    ) -> None:
        run = harness.full()
        content = harness.artifact(
            run.deliberation_id, DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
        ).content

        assert content["upstream_fingerprints"] == [
            harness.artifact(run.deliberation_id, stage, slot).fingerprint
            for stage, slot in (
                (DeliberationStage.AGENT_A_ROUND1, SLOT_AGENT_A),
                (DeliberationStage.AGENT_B_ROUND1, SLOT_AGENT_B),
                (DeliberationStage.LEAD_REVIEW, SLOT_LEAD),
                (DeliberationStage.AGENT_A_ROUND2, SLOT_AGENT_A),
                (DeliberationStage.AGENT_B_ROUND2, SLOT_AGENT_B),
            )
        ]
        assert content["synthesis_id"] == harness.engine.snapshot(
            run.deliberation_id
        ).final_synthesis_id

    def test_the_synthesis_requires_the_round_2_answers(self, harness: Harness) -> None:
        run = harness.round1(harness.start())
        run = harness.lead_review(run)

        with pytest.raises(DeliberationStageOrderError):
            harness.synthesis(run)
        assert harness.lead.syntheses == []

    def test_a_failing_chair_leaves_no_synthesis(self) -> None:
        harness = Harness(lead=ScriptedLead(fail_synthesis=True))
        run = harness.round1(harness.start())
        run = harness.lead_review(run)
        run = harness.round2(run)
        run = harness.synthesis(run)

        assert run.status is DeliberationStatus.ERROR
        assert run.error_reason.startswith("lead provider failed")
        assert harness.artifact(
            run.deliberation_id, DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
        ).status is DeliberationStageStatus.ERROR

    def test_a_malformed_synthesis_fails_closed(self) -> None:
        harness = Harness(
            lead=ScriptedLead(synthesis_raw={"summary": "   ", "modules": []})
        )
        run = harness.round1(harness.start())
        run = harness.lead_review(run)
        run = harness.round2(run)
        run = harness.synthesis(run)

        assert run.status is DeliberationStatus.ERROR
        assert "malformed answer" in run.error_reason


# ---------------------------------------------------------------------------
# the proposal: exact linkage, human approval remains mandatory
# ---------------------------------------------------------------------------

class TestProposal:
    """Final synthesis -> DRAFT proposal, linked to the exact synthesis identity."""

    def test_the_proposal_is_a_draft_linked_to_the_exact_synthesis(
        self, harness: Harness
    ) -> None:
        run = harness.full()
        proposal = harness.proposal(run, architecture_version="1.1")
        synthesis = harness.artifact(
            run.deliberation_id, DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
        )

        assert proposal.status is ProposalStatus.DRAFT
        assert proposal.source_review_id == synthesis.content["synthesis_id"]
        assert (
            proposal.review_digest["synthesis_id"]
            == synthesis.content["synthesis_id"]
        )
        assert proposal.review_digest["deliberation_id"] == run.deliberation_id
        assert proposal.requirement == REQUIREMENT
        assert proposal.project == "youtube_to_mp3"
        assert fingerprint_matches(proposal) is True

    def test_the_proposal_carries_the_synthesis_content(
        self, harness: Harness
    ) -> None:
        run = harness.full()
        proposal = harness.proposal(run, architecture_version="1.1")

        assert proposal.summary
        assert proposal.modules
        assert proposal.risks
        assert proposal.adr_candidates
        assert proposal.implementation_phases
        assert proposal.unresolved_questions

    def test_a_proposal_is_never_approved_by_the_deliberation(
        self, harness: Harness
    ) -> None:
        run = harness.full()
        proposal = harness.proposal(run)

        assert proposal.status is ProposalStatus.DRAFT
        assert proposal.decided_by == ""
        assert proposal.decided_at is None
        assert not hasattr(harness.engine, "approve")

    def test_a_proposal_is_only_possible_after_ready_for_proposal(
        self, harness: Harness
    ) -> None:
        run = harness.round1(harness.start())

        with pytest.raises(DeliberationStageOrderError):
            harness.proposal(run)
        assert harness.proposals.items == {}

    def test_the_proposal_and_its_audit_entry_share_one_transaction(
        self, harness: Harness
    ) -> None:
        run = harness.full()
        opened_before = harness.transactions.opened
        proposal = harness.proposal(run)

        assert harness.transactions.opened == opened_before + 1
        entries = [
            entry
            for entry in harness.audit.entries
            if entry.entity_id == proposal.proposal_id
        ]
        assert len(entries) == 1
        assert entries[0].entity_type is AuditEntityType.PROPOSAL
        assert entries[0].detail["deliberation_id"] == run.deliberation_id

    def test_a_tampered_synthesis_cannot_produce_a_proposal(
        self, harness: Harness
    ) -> None:
        from dataclasses import replace as dataclass_replace

        run = harness.full()
        synthesis = harness.artifact(
            run.deliberation_id, DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
        )
        tampered = dict(synthesis.content)
        tampered["summary"] = "hand-edited"
        harness.deliberations.upsert_artifact(
            dataclass_replace(synthesis, content=tampered)
        )

        with pytest.raises(DeliberationStaleError):
            harness.proposal(run)
        assert harness.proposals.items == {}

    def test_a_rewritten_upstream_artifact_makes_the_synthesis_stale(
        self, harness: Harness
    ) -> None:
        from dataclasses import replace as dataclass_replace

        run = harness.full()
        round1 = harness.artifact(
            run.deliberation_id, DeliberationStage.AGENT_A_ROUND1, SLOT_AGENT_A
        )
        rewritten = dict(round1.content)
        rewritten["proposal_summary"] = "rewritten later"
        harness.deliberations.upsert_artifact(
            dataclass_replace(round1, content=rewritten)
        )

        with pytest.raises(DeliberationStaleError):
            harness.proposal(run)

    def test_a_hand_edited_run_identity_is_refused(self, harness: Harness) -> None:
        from dataclasses import replace as dataclass_replace

        run = harness.full()
        harness.deliberations.upsert_run(
            dataclass_replace(run, requirement=REQUIREMENT + " tampered")
        )

        with pytest.raises(DeliberationStaleError):
            harness.proposal(run)

    def test_a_changed_seat_configuration_is_refused_mid_run(
        self, harness: Harness
    ) -> None:
        run = harness.round1(harness.start())
        harness.engine.configure(
            agent_a=DeliberationSeat(
                SLOT_AGENT_A, "grok", "grok-4.6", harness.agent_a
            )
        )

        with pytest.raises(DeliberationStaleError):
            harness.lead_review(run)

    def test_a_changed_context_is_refused_mid_run(self, harness: Harness) -> None:
        run = harness.start(context={"baseline": "1.1"})

        with pytest.raises(DeliberationStaleError):
            harness.engine.run_round1(
                run.deliberation_id,
                actor=ACTOR,
                reason=REASON,
                context={"baseline": "1.2"},
            )

    def test_a_revision_runs_as_a_new_deliberation(self, harness: Harness) -> None:
        run = harness.full()
        harness.proposal(run)

        revision = harness.engine.start(
            REQUIREMENT,
            actor=ACTOR,
            reason="human feedback: keep the CLI usable",
            revision_of=run.deliberation_id,
            on_event=harness.sink,
        )

        assert revision.deliberation_id != run.deliberation_id
        assert revision.revision_no == 2
        assert revision.revision_of_deliberation_id == run.deliberation_id
        stored = harness.deliberations.get_run(run.deliberation_id)
        assert stored.status is DeliberationStatus.READY_FOR_PROPOSAL

    def test_only_a_settled_run_may_be_superseded(self, harness: Harness) -> None:
        run = harness.round1(harness.start())

        with pytest.raises(DeliberationError):
            harness.engine.start(
                REQUIREMENT,
                actor=ACTOR,
                reason="too early",
                revision_of=run.deliberation_id,
            )


class TestGuards:
    """Reads fail closed and invalid wiring is refused."""

    def test_an_unknown_deliberation_is_refused(self, harness: Harness) -> None:
        with pytest.raises(DeliberationNotFoundError):
            harness.engine.require("nope")
        with pytest.raises(DeliberationNotFoundError):
            harness.engine.snapshot("nope")
        with pytest.raises(DeliberationError):
            harness.engine.require("   ")

    def test_the_collaborators_are_validated(self) -> None:
        seat_a = DeliberationSeat(
            SLOT_AGENT_A, "deepseek", "m", ScriptedAgent("d", "A")
        )
        seat_b = DeliberationSeat(
            SLOT_AGENT_B, "claude", "m", ScriptedAgent("c", "B")
        )
        chair = DeliberationChair("openai", "m", ScriptedLead())

        def build(**kwargs):
            return ArchitectureDeliberation(
                kwargs.get("deliberations", FakeDeliberations()),
                kwargs.get("proposals", FakeProposals()),
                kwargs.get("projects", FakeProjects()),
                kwargs.get("audit", FakeAudit()),
                kwargs.get("transactions", FakeTransactions()),
                agent_a=kwargs.get("agent_a", seat_a),
                agent_b=kwargs.get("agent_b", seat_b),
                chair=kwargs.get("chair", chair),
            )

        with pytest.raises(ValueError):
            build(deliberations=object())
        with pytest.raises(ValueError):
            build(proposals=object())
        with pytest.raises(ValueError):
            build(audit=object())
        with pytest.raises(ValueError):
            build(agent_a="not a seat")
        with pytest.raises(ValueError):
            build(agent_a=seat_b, agent_b=seat_a)
        with pytest.raises(ValueError):
            build(chair=object())

    def test_a_seat_rejects_an_unknown_slot(self) -> None:
        with pytest.raises(DeliberationError):
            DeliberationSeat(SLOT_LEAD, "openai", "m", ScriptedAgent("o", "A"))

    def test_a_chair_requires_an_implementation_with_two_operations(self) -> None:
        with pytest.raises(DeliberationError):
            DeliberationChair("openai", "m", object())


# ---------------------------------------------------------------------------
# disabled seats, cost, events, snapshots
# ---------------------------------------------------------------------------

class TestDisabledSeat:
    """A disabled seat abstains with no call at all."""

    def test_a_disabled_seat_abstains_without_a_call(self) -> None:
        harness = Harness(agent_b_seat=("disabled", ""), agent_b_seat_port=None)
        run = harness.round1(harness.start())
        artifact = harness.artifact(
            run.deliberation_id, DeliberationStage.AGENT_B_ROUND1, SLOT_AGENT_B
        )

        assert artifact.status is DeliberationStageStatus.ABSTAINED
        assert "no call was made" in artifact.content["reason"]
        assert harness.agent_b.analyses == []

    def test_a_run_without_a_usable_peer_never_claims_a_deliberation(self) -> None:
        harness = Harness(agent_b_seat=("disabled", ""), agent_b_seat_port=None)
        run = harness.round1(harness.start())
        run = harness.lead_review(run)

        assert run.status is DeliberationStatus.ERROR
        assert harness.lead.reviews == []


class TestCost:
    """Cost is attributed per stage, and a missing figure stays missing."""

    def test_each_stage_reports_its_own_cost(self, harness: Harness) -> None:
        run = harness.full()
        snapshot = harness.engine.snapshot(run.deliberation_id)

        assert len(snapshot.cost["stages"]) == 6
        assert {entry["stage"] for entry in snapshot.cost["stages"]} == {
            stage.value for stage in DeliberationStage
        }
        assert snapshot.cost["available"] is True
        assert snapshot.cost["priced_stages"] == 6
        assert snapshot.cost["total_usd"] == pytest.approx(0.14)

    def test_an_unreported_cost_stays_unavailable(self) -> None:
        harness = Harness(
            agent_a=ScriptedAgent("deepseek", "A", cost={}),
            agent_b=ScriptedAgent("claude", "B", cost={}),
        )
        run = harness.full()
        snapshot = harness.engine.snapshot(run.deliberation_id)

        assert snapshot.cost["available"] is True  # the chair did report one
        assert snapshot.cost["unpriced_stages"] >= 2
        for entry in snapshot.cost["stages"]:
            if not entry["available"]:
                assert entry["total_usd"] is None

    def test_no_cost_at_all_is_unavailable_never_zero(self) -> None:
        harness = Harness(
            agent_a=ScriptedAgent("deepseek", "A", cost={}),
            agent_b=ScriptedAgent("claude", "B", cost={}),
            lead=ScriptedLead(cost={}),
        )
        run = harness.full()
        snapshot = harness.engine.snapshot(run.deliberation_id)

        assert snapshot.cost["available"] is False
        assert snapshot.cost["total_usd"] is None
        assert snapshot.cost["reason"] == "no stage reported cost telemetry"

    def test_the_cost_is_carried_into_the_proposal_digest(
        self, harness: Harness
    ) -> None:
        run = harness.full()
        proposal = harness.proposal(run)

        assert proposal.review_digest["cost"]["available"] is True


class TestEventsAndSafety:
    """The event vocabulary, and the promise that no secret ever travels."""

    def test_the_full_protocol_emits_the_expected_actions(
        self, harness: Harness
    ) -> None:
        harness.full()

        assert harness.event_actions() == [
            "deliberation-created",
            "round1-started",
            "agent-a-round1-started",
            "agent-a-round1-completed",
            "agent-b-round1-started",
            "agent-b-round1-completed",
            "round1-completed",
            "lead-review-started",
            "lead-review-completed",
            "round2-started",
            "agent-a-round2-started",
            "agent-a-round2-completed",
            "agent-b-round2-started",
            "agent-b-round2-completed",
            "round2-completed",
            "final-synthesis-started",
            "final-synthesis-completed",
        ]

    def test_every_event_is_json_safe_and_carries_no_secret(
        self, harness: Harness
    ) -> None:
        run = harness.full()
        harness.proposal(run)
        serialized = json.dumps(harness.events, sort_keys=True).casefold()

        for word in ("api_key", "authorization", "bearer ", "sk-"):
            assert word not in serialized, word

    def test_no_credential_reaches_a_run_artifact_audit_or_proposal(
        self, harness: Harness
    ) -> None:
        run = harness.full()
        harness.proposal(run)
        payload = {
            "run": run.to_dict(),
            "artifacts": [
                artifact.to_dict()
                for artifact in harness.engine.artifacts(run.deliberation_id)
            ],
            "proposals": [
                proposal.to_dict() for proposal in harness.proposals.list()
            ],
            "audit": [dict(entry.detail) for entry in harness.audit.entries],
        }

        assert "api_key" not in json.dumps(payload, sort_keys=True)


class TestSnapshotAndActions:
    """The panel's read-only view, and only the stage-valid controls."""

    def test_a_fresh_run_offers_only_round_1(self, harness: Harness) -> None:
        run = harness.start()
        snapshot = harness.engine.snapshot(run.deliberation_id)

        assert snapshot.available_actions == (ACTION_RUN_ROUND1, ACTION_CANCEL)
        assert snapshot.ready_for_proposal is False
        assert snapshot.final_synthesis_id is None
        assert (
            snapshot.stages[DeliberationStage.FINAL_SYNTHESIS.value]["status"]
            == DeliberationStageStatus.PENDING.value
        )

    def test_each_stage_offers_exactly_its_own_next_action(
        self, harness: Harness
    ) -> None:
        run = harness.round1(harness.start())
        assert harness.actions(run.deliberation_id) == (
            ACTION_GENERATE_LEAD_REVIEW,
            ACTION_CANCEL,
        )

        run = harness.lead_review(run)
        assert harness.actions(run.deliberation_id) == (
            ACTION_RUN_ROUND2,
            ACTION_CANCEL,
        )

        run = harness.round2(run)
        assert harness.actions(run.deliberation_id) == (
            ACTION_GENERATE_FINAL_SYNTHESIS,
            ACTION_CANCEL,
        )

    def test_a_finished_run_offers_only_the_proposal(self, harness: Harness) -> None:
        run = harness.full()
        snapshot = harness.engine.snapshot(run.deliberation_id)

        assert snapshot.available_actions == (
            ACTION_GENERATE_PROPOSAL,
            ACTION_CANCEL,
        )
        assert snapshot.ready_for_proposal is True
        assert snapshot.stale_reason == ""
        assert snapshot.counts["agreements"] == 1
        assert snapshot.counts["conflicts"] == 1
        assert snapshot.counts["risks"] == 1

    def test_the_snapshot_is_json_safe(self, harness: Harness) -> None:
        run = harness.full()
        snapshot = harness.engine.snapshot(run.deliberation_id)

        assert isinstance(json.dumps(snapshot.to_dict(), sort_keys=True), str)
        assert snapshot.seats[SLOT_AGENT_A]["provider"] == "deepseek"
        assert snapshot.seats[SLOT_LEAD]["provider"] == "openai"

    def test_a_stale_synthesis_is_visible_in_the_snapshot(
        self, harness: Harness
    ) -> None:
        from dataclasses import replace as dataclass_replace

        run = harness.full()
        synthesis = harness.artifact(
            run.deliberation_id, DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
        )
        tampered = dict(synthesis.content)
        tampered["summary"] = "edited by hand"
        harness.deliberations.upsert_artifact(
            dataclass_replace(synthesis, content=tampered)
        )

        assert harness.engine.snapshot(run.deliberation_id).stale_reason != ""

    def test_cancel_stops_a_run_and_offers_no_further_cancel(
        self, harness: Harness
    ) -> None:
        run = harness.round1(harness.start())
        cancelled = harness.engine.cancel(
            run.deliberation_id,
            actor=ACTOR,
            reason="operator stopped it",
            on_event=harness.sink,
        )

        assert cancelled.status is DeliberationStatus.CANCELLED
        assert "deliberation-cancelled" in harness.event_actions()
        assert ACTION_CANCEL not in harness.actions(run.deliberation_id)

    def test_a_finished_run_cannot_be_cancelled(self, harness: Harness) -> None:
        run = harness.full()

        with pytest.raises(DeliberationStageOrderError):
            harness.engine.cancel(run.deliberation_id, actor=ACTOR, reason="late")

    def test_an_error_run_offers_the_retry_of_the_failed_stage(self) -> None:
        harness = Harness(lead=ScriptedLead(fail_review=True))
        run = harness.round1(harness.start())
        run = harness.lead_review(run)

        assert harness.actions(run.deliberation_id) == (
            ACTION_GENERATE_LEAD_REVIEW,
            ACTION_CANCEL,
        )

    def test_an_operator_may_retry_a_failed_stage(self) -> None:
        lead = ScriptedLead(fail_review=True)
        harness = Harness(lead=lead)
        run = harness.round1(harness.start())
        run = harness.lead_review(run)
        assert run.status is DeliberationStatus.ERROR

        lead.fail_review = False
        run = harness.lead_review(run)

        assert run.status is DeliberationStatus.LEAD_REVIEW_COMPLETE
        assert len(lead.reviews) == 2














