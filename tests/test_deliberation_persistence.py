"""Step 29 persistence, adapter and offline end-to-end tests.

Three families, and they are the ones that decide whether the deliberation is
*durable* rather than merely demonstrated:

* **durability.** Against real SQLite a completed run, its six stage artifacts
  and its proposal survive a reopen, and a stage is individually addressable -
  that is what makes the two tables a lifecycle rather than one opaque document;
* **provider neutrality.** The real adapters speak the OpenAI-compatible and the
  Anthropic dialects, and a disabled provider resolves to ``None`` (no adapter,
  no call);
* **the offline end-to-end.** The whole protocol runs through ``compose()`` with
  scripted transports and produces a ``DRAFT`` proposal - with no approval, no
  Cline task and no supervisor path anywhere in the run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from architecture_assistant.composition import (
    AdvisorFactory,
    CompositionConfig,
    ProviderSelection,
    assemble_deliberation,
    compose,
    default_provider_settings,
    probe_deliberation,
    provider_catalog,
)
from architecture_assistant.domain.enums import (
    ProposalStatus,
    DeliberationStage,
    DeliberationStatus,
)
from architecture_assistant.infrastructure import (
    DELIBERATION_ARTIFACT_TABLE_NAME,
    DELIBERATION_RUN_TABLE_NAME,
    DeliberationAgentAdapter,
    DeliberationInvalidResponseError,
    DeliberationLeadAdapter,
    DeliberationMissingApiKeyError,
    SqliteStorage,
    close_database,
    open_database,
)
from architecture_assistant.infrastructure._http import (
    HttpTransportError,
    HttpResponse,
)
from architecture_assistant.ports.capabilities import (
    SLOT_AGENT_A,
    SLOT_AGENT_B,
    AgentAnalysisQuery,
    LeadReviewQuery,
)

REQUIREMENT = "MP3 fails -> TXT. Vienkarsa Windows Python programma ar GUI."


def chat_body(payload) -> bytes:
    """One OpenAI-compatible chat-completions response."""
    return json.dumps(
        {
            "choices": [{"message": {"content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
    ).encode("utf-8")


def messages_body(payload) -> bytes:
    """One Anthropic messages response."""
    return json.dumps(
        {
            "content": [{"text": json.dumps(payload)}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    ).encode("utf-8")


def transport_for(body: bytes, calls: list | None = None):
    """A transport that always answers with ``body`` and counts its calls."""

    def transport(request):
        if calls is not None:
            calls.append(request)
        return HttpResponse(status_code=200, body=body)

    return transport


# ---------------------------------------------------------------------------
# the real adapters
# ---------------------------------------------------------------------------

class TestAdapters:
    """The four providers, both dialects, and the fail-closed paths."""

    def test_the_chat_dialect_serves_openai_grok_and_deepseek(self) -> None:
        body = chat_body({"proposal_summary": "plan", "modules": []})

        for provider in ("openai", "grok", "deepseek"):
            adapter = DeliberationAgentAdapter(
                provider, "a-model", "a-key", transport=transport_for(body)
            )
            result = adapter.analyse(
                AgentAnalysisQuery(
                    project="p",
                    requirement="r",
                    deliberation_id="d",
                    slot=SLOT_AGENT_A,
                )
            )
            assert result.source == provider
            assert result.content["proposal_summary"] == "plan"
            assert result.cost == {} or result.cost.get("available") is False

    def test_the_messages_dialect_serves_claude(self) -> None:
        body = messages_body({"agreements": ["a"], "conflicts": []})
        adapter = DeliberationLeadAdapter(
            "claude", "a-model", "a-key", transport=transport_for(body)
        )
        result = adapter.review(
            LeadReviewQuery(project="p", requirement="r", deliberation_id="d")
        )

        assert result.source == "claude"
        assert result.content["agreements"] == ["a"]

    def test_a_missing_credential_makes_no_call(self) -> None:
        calls: list = []
        adapter = DeliberationAgentAdapter(
            "openai", "m", "", transport=transport_for(b"{}", calls=calls)
        )

        assert adapter.is_configured is False
        with pytest.raises(DeliberationMissingApiKeyError):
            adapter._headers()
        assert calls == []

    def test_a_malformed_answer_fails_closed(self) -> None:
        adapter = DeliberationAgentAdapter(
            "openai", "m", "k", transport=transport_for(chat_body("not an object"))
        )

        with pytest.raises(DeliberationInvalidResponseError):
            adapter.analyse(
                AgentAnalysisQuery(
                    project="p",
                    requirement="r",
                    deliberation_id="d",
                    slot=SLOT_AGENT_A,
                )
            )

    def test_a_transport_error_is_bounded_to_one_retry(self) -> None:
        attempts: list = []

        def failing(request):
            attempts.append(request)
            raise HttpTransportError("down")

        adapter = DeliberationAgentAdapter(
            "openai",
            "m",
            "k",
            transport=failing,
            max_retries=1,
            sleep=lambda _seconds: None,
        )

        with pytest.raises(Exception):
            adapter.analyse(
                AgentAnalysisQuery(
                    project="p",
                    requirement="r",
                    deliberation_id="d",
                    slot=SLOT_AGENT_A,
                )
            )
        assert len(attempts) == 2  # one attempt plus exactly one bounded retry

    def test_a_refused_credential_is_never_retried(self) -> None:
        attempts: list = []

        def refusing(request):
            attempts.append(request)
            return HttpResponse(status_code=401, body=b"{}")

        adapter = DeliberationAgentAdapter(
            "openai",
            "m",
            "k",
            transport=refusing,
            max_retries=3,
            sleep=lambda _seconds: None,
        )

        with pytest.raises(DeliberationMissingApiKeyError):
            adapter.analyse(
                AgentAnalysisQuery(
                    project="p",
                    requirement="r",
                    deliberation_id="d",
                    slot=SLOT_AGENT_A,
                )
            )
        assert len(attempts) == 1

    def test_the_probe_answers_a_verdict_word(self) -> None:
        body = chat_body({"ok": True})
        ok = DeliberationLeadAdapter(
            "openai", "m", "k", transport=transport_for(body)
        )
        no_key = DeliberationLeadAdapter(
            "openai", "m", "", transport=transport_for(body)
        )

        assert ok._test_connection() == "CONNECTED"
        assert no_key._test_connection() == "AUTH ERROR"
        assert probe_deliberation("disabled") == "DISABLED"
        assert (
            probe_deliberation("openai", "m", "k", transport=transport_for(body))
            == "CONNECTED"
        )

    def test_a_disabled_seat_resolves_to_no_adapter_at_all(self) -> None:
        settings = default_provider_settings().with_selection(
            "lead", ProviderSelection("disabled")
        )
        agent_a, agent_b, chair = assemble_deliberation(settings)

        assert chair.lead is None
        assert chair.enabled is False
        assert agent_a.agent is not None and agent_b.agent is not None

    def test_the_provider_catalog_still_lists_every_provider(self) -> None:
        catalog = provider_catalog()

        assert {entry["provider"] for entry in catalog} >= {
            "openai",
            "claude",
            "grok",
            "deepseek",
            "disabled",
        }
        assert AdvisorFactory is not None


# ---------------------------------------------------------------------------
# durability against real SQLite
# ---------------------------------------------------------------------------

class TestDurability:
    """A run, its stages and its proposal survive a reopen - addressably."""

    def test_the_deliberation_tables_exist(self, tmp_path: Path) -> None:
        connection = open_database(tmp_path / "deliberation.db")
        try:
            names = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            close_database(connection)

        assert DELIBERATION_RUN_TABLE_NAME in names
        assert DELIBERATION_ARTIFACT_TABLE_NAME in names

    def test_a_completed_run_survives_a_reopen(self, tmp_path: Path) -> None:
        database = tmp_path / "deliberation.db"
        composition = compose(_config(database))
        try:
            engine = composition.architecture_deliberation
            run = _drive(engine)
            proposal = engine.generate_proposal(
                run.deliberation_id, actor="operator", reason="durability"
            )
            deliberation_id = run.deliberation_id
            proposal_id = proposal.proposal_id
            synthesis_id = engine.snapshot(deliberation_id).final_synthesis_id
        finally:
            composition.close()

        reopened = compose(_config(database))
        try:
            storage = reopened.storage
            stored = storage.deliberations.get_run(deliberation_id)
            artifacts = storage.deliberations.list_artifacts(deliberation_id)
            assert stored is not None
            assert stored.status is DeliberationStatus.READY_FOR_PROPOSAL
            assert stored.requirement == REQUIREMENT
            # every stage is its own addressable row
            assert {item.stage for item in artifacts} == set(DeliberationStage)
            assert all(item.fingerprint for item in artifacts)
            # the proposal points at the exact synthesis, and is still a DRAFT
            stored_proposal = storage.proposals.get(proposal_id)
            assert stored_proposal is not None
            assert stored_proposal.status is ProposalStatus.DRAFT
            assert stored_proposal.source_review_id == synthesis_id
            # stage cost attribution survived too
            assert any(
                item.content.get("cost")
                for item in artifacts
                if item.stage is DeliberationStage.LEAD_REVIEW
            )
            # and no credential is anywhere in the persisted rows
            assert "api_key" not in _row_text(reopened.connection)
        finally:
            reopened.close()

    def test_a_stage_is_individually_addressable(self, tmp_path: Path) -> None:
        composition = compose(_config(tmp_path / "stage.db"))
        try:
            engine = composition.architecture_deliberation
            run = _drive(engine)
            repository = composition.storage.deliberations
            stage = repository.list_artifacts_for_stage(
                run.deliberation_id, DeliberationStage.AGENT_A_ROUND1, SLOT_AGENT_A
            )

            assert len(stage) == 1
            assert stage[0].artifact_id.startswith("agent-a-round1-")
            assert stage[0].content["proposal_summary"]
            assert (
                repository.get_artifact(stage[0].artifact_id).artifact_id
                == stage[0].artifact_id
            )
        finally:
            composition.close()


def _config(database: Path) -> CompositionConfig:
    """One composition config over a temporary database."""
    return CompositionConfig(
        database_path=database, source_root=Path("src/architecture_assistant")
    )


def _row_text(connection) -> str:
    """Every cell of the deliberation tables, as one string - a leak check."""
    chunks: list[str] = []
    for table in (DELIBERATION_RUN_TABLE_NAME, DELIBERATION_ARTIFACT_TABLE_NAME):
        for row in connection.execute(f"SELECT * FROM {table}"):
            chunks.append(" ".join(str(value) for value in tuple(row)))
    return " ".join(chunks)


def _drive(engine) -> object:
    """Run the whole protocol with the composition's seats replaced by scripts."""
    from architecture_assistant.application import (
        DeliberationChair,
        DeliberationSeat,
    )
    from architecture_assistant.ports.capabilities import SLOT_AGENT_B
    from tests.test_architecture_deliberation import ScriptedAgent, ScriptedLead

    engine.configure(
        agent_a=DeliberationSeat(
            SLOT_AGENT_A,
            "deepseek",
            "deepseek-chat",
            ScriptedAgent("deepseek", "A"),
        ),
        agent_b=DeliberationSeat(
            SLOT_AGENT_B, "claude", "claude-x", ScriptedAgent("claude", "B")
        ),
        chair=DeliberationChair("openai", "gpt-5.6", ScriptedLead()),
    )
    run = engine.start(REQUIREMENT, actor="operator", reason="durability")
    for stage in (
        engine.run_round1,
        engine.generate_lead_review,
        engine.run_round2,
        engine.generate_final_synthesis,
    ):
        run = stage(run.deliberation_id, actor="operator", reason="durability")
    return run


# ---------------------------------------------------------------------------
# the offline end-to-end: real adapters, scripted transports, real SQLite
# ---------------------------------------------------------------------------

ROUND1_BODY = chat_body(
    {
        "proposal_summary": "A CLI core behind a thin GUI.",
        "modules": [{"name": "core", "responsibility": "transcribe"}],
        "risks": [{"title": "ffmpeg availability"}],
        "open_questions": ["which codec?"],
        "confidence": 0.6,
    }
)
LEAD_REVIEW_BODY = messages_body(
    {
        "agreements": ["both want a CLI core"],
        "conflicts": ["local engine vs remote API"],
        "questions_for_agent_a": ["which assumption is weakest?"],
        "unresolved_questions": ["which codec?"],
    }
)
ROUND2_BODY = chat_body(
    {
        "decision": "REVISE",
        "revised_summary": "A CLI core with a pluggable engine.",
        "changed_decisions": [{"what": "engine behind a port"}],
        "remaining_disagreements": ["local vs remote transcription"],
        "remaining_questions": ["who owns the temp files?"],
    }
)
SYNTHESIS_BODY = messages_body(
    {
        "summary": "A Windows GUI over a reusable CLI transcription core.",
        "modules": [{"name": "core", "responsibility": "transcribe"}],
        "risks": [{"title": "codec availability"}],
        "remaining_disagreements": [],
        "unresolved_questions": ["which codec?"],
        "rationale": "the engine boundary keeps the GUI replaceable",
    }
)


class ScriptedChair:
    """One chair that answers the review and the synthesis differently.

    A real adapter is single-answer by construction, so this wraps two of them:
    the point of the end-to-end test is the *use-case* path, not the transport.
    """

    def __init__(self, reviewer, synthesiser) -> None:
        self._reviewer = reviewer
        self._synthesiser = synthesiser
        self.reviews = 0
        self.syntheses = 0

    def review(self, query):
        self.reviews += 1
        return self._reviewer.review(query)

    def synthesize(self, query):
        self.syntheses += 1
        return self._synthesiser.synthesize(query)


def _seat(slot: str, provider: str, model: str, body: bytes):
    from architecture_assistant.application import DeliberationSeat

    return DeliberationSeat(
        slot,
        provider,
        model,
        DeliberationAgentAdapter(
            provider, model, "k", transport=transport_for(body)
        ),
    )


def _chair():
    from architecture_assistant.application import DeliberationChair

    return DeliberationChair(
        "claude",
        "claude-x",
        ScriptedChair(
            DeliberationLeadAdapter(
                "claude", "claude-x", "k",
                transport=transport_for(LEAD_REVIEW_BODY),
            ),
            DeliberationLeadAdapter(
                "claude", "claude-x", "k",
                transport=transport_for(SYNTHESIS_BODY),
            ),
        ),
    )


class TestOfflineEndToEnd:
    """The whole protocol, with real adapters and no network at all."""

    def test_the_full_protocol_produces_a_draft_proposal(
        self, tmp_path: Path
    ) -> None:
        composition = compose(_config(tmp_path / "e2e.db"))
        try:
            engine = composition.architecture_deliberation
            engine.configure(
                agent_a=_seat(
                    SLOT_AGENT_A, "deepseek", "deepseek-chat", ROUND1_BODY
                ),
                agent_b=_seat(SLOT_AGENT_B, "grok", "grok-4.6", ROUND1_BODY),
                chair=_chair(),
            )
            run = engine.start(REQUIREMENT, actor="operator", reason="e2e")
            run = engine.run_round1(
                run.deliberation_id, actor="operator", reason="e2e"
            )
            run = engine.generate_lead_review(
                run.deliberation_id, actor="operator", reason="e2e"
            )
            engine.configure(
                agent_a=_seat(
                    SLOT_AGENT_A, "deepseek", "deepseek-chat", ROUND2_BODY
                ),
                agent_b=_seat(SLOT_AGENT_B, "grok", "grok-4.6", ROUND2_BODY),
            )
            run = engine.run_round2(
                run.deliberation_id, actor="operator", reason="e2e"
            )
            run = engine.generate_final_synthesis(
                run.deliberation_id, actor="operator", reason="e2e"
            )
            snapshot = engine.snapshot(run.deliberation_id)
            proposal = engine.generate_proposal(
                run.deliberation_id, actor="operator", reason="e2e"
            )

            assert run.status is DeliberationStatus.READY_FOR_PROPOSAL
            assert snapshot.counts["conflicts"] == 1
            # the seats' own disagreement survives the chair's empty list
            assert any(
                "local vs remote" in item
                for item in snapshot.artifacts[-1]["content"][
                    "remaining_disagreements"
                ]
            )
            assert proposal.status is ProposalStatus.DRAFT
            assert proposal.decided_by == ""
            assert proposal.review_digest["source"] == "architecture-deliberation"
            assert not any(
                entry.entity_type.value == "STEP"
                for entry in composition.storage.audit.list()
            ), "a deliberation never touches a Step"
        finally:
            composition.close()

    def test_no_cline_task_and_no_supervisor_path_is_reached(
        self, tmp_path: Path
    ) -> None:
        composition = compose(_config(tmp_path / "e2e2.db"))
        try:
            storage = composition.storage
            before = (
                len(storage.tasks.list()),
                len(storage.supervisions.list()),
                len(storage.steps.list()),
            )
            _drive(composition.architecture_deliberation)
            after = (
                len(storage.tasks.list()),
                len(storage.supervisions.list()),
                len(storage.steps.list()),
            )

            assert before == after
        finally:
            composition.close()




