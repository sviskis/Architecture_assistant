"""Tests for the GUI controller: the button matrix, gating and threading.

The controller is pure logic over plain payloads, so everything here runs without
a display and without a core: a fake runner records the jobs and a fake worker
answers them. The enable/disable matrix is asserted against the *authoritative*
transition table, so the GUI cannot advertise an action the core would refuse.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from architecture_assistant.domain.enums import StepEvent, StepState
from architecture_assistant.domain.fsm import STEP_TRANSITIONS
from architecture_assistant_gui.controller import (
    ABORT_STATES,
    APPROVAL_STATES,
    INTENTS,
    RESOLVE_STATES,
    UNBLOCK_STATES,
    GuiController,
)
from architecture_assistant_gui.core import (
    JobResult,
    describe_error,
    is_critical,
)


def _states_for(event: StepEvent) -> frozenset[str]:
    """The states from which the authoritative table allows ``event``."""
    return frozenset(
        state.value
        for (state, candidate) in STEP_TRANSITIONS
        if candidate is event
    )


def make_payload(
    *,
    state: Optional[str] = None,
    paused: bool = False,
    mode: str = "AUTO",
    attempt: int = 1,
    max_attempts: int = 3,
    tasks: Optional[list[dict[str, Any]]] = None,
    reviewers: Optional[list[str]] = None,
) -> dict[str, Any]:
    """A canonical projection payload, as the worker would send it."""
    steps: list[dict[str, Any]] = (
        []
        if state is None
        else [
            {
                "step_no": 9,
                "phase": "LOOP",
                "title": "GUI step",
                "description": "",
                "state": state,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "risk": "LOW",
                "requires_human": False,
            }
        ]
    )
    project = {"name": "Project", "mode": mode, "paused": paused}
    health = {
        "project_paused": paused,
        "step_count": len(steps),
        "current_step_no": 9 if steps else None,
        "current_state": state,
        "next_step_no": None,
        "complete": False,
    }
    return {
        "canonical": {
            "schema_version": "1.0",
            "generated_at": "2026-09-21T22:00:00+00:00",
            "project": project,
            "steps": steps,
            "tasks": list(tasks or []),
        },
        "project": project,
        "health": health,
        "steps": steps,
        "tasks": list(tasks or []),
        "cost": {"total_usd": 0.0, "record_count": 0},
        "architecture": {"version": "1.1"},
        "architecture_version": "1.1",
        "current_step": steps[0] if steps else None,
        "next_step": None,
        "blocking_steps": (
            [{"step_no": 9, "state": state}] if state == "BLOCKED" else []
        ),
        "open_risks": [],
        "change_requests": [{"request_id": "ACR-001"}],
        "reviewers": (
            ["OpenAI", "Claude", "Grok"] if reviewers is None else reviewers
        ),
        "paths": {
            "database_path": "data/a.db",
            "exchange_dir": "data/cline",
            "report_dir": "reports",
            "source_root": "src/architecture_assistant",
        },
    }


def make_preview(**overrides: Any) -> dict[str, Any]:
    """The plan preview the core would send, as plain data."""
    preview: dict[str, Any] = {
        "valid": True,
        "importable": True,
        "blocked_reason": "",
        "issues": [],
        "project_name": "Project",
        "mode": "AUTO",
        "plan_version": "0.3",
        "plan_version_declared": True,
        "plan_hash": "a" * 64,
        "step_count": 2,
        "first_step_no": 1,
        "last_step_no": 2,
        "risk_counts": {"LOW": 1, "MEDIUM": 1, "HIGH": 0},
        "requires_human_count": 1,
        "phases": ["CONTEXT", "ARCHITECTURE"],
        "db_project_name": "Project",
        "db_plan_version": "0.3",
        "db_mode": "AUTO",
        "db_step_count": 0,
        "project_matches": True,
        "plan_version_matches": True,
        "mode_matches": True,
        "source_file": "C:/plans/youtube_to_mp3.json",
    }
    preview.update(overrides)
    return preview


#: The plan text the panel would hand to the core (never parsed by the panel).
PLAN_TEXT = '{"schema_version": "1.0", "steps": []}'

#: The review question the operator is expected to be able to write himself.
REVIEW_QUESTION = (
    "Review the youtube_to_mp3 architecture. Identify module boundaries, "
    "dependency risks, failure points and the simplest maintainable design."
)


def make_review(**overrides: Any) -> dict[str, Any]:
    """The review payload the core would send, as plain data."""
    payload: dict[str, Any] = {
        "review_id": "architecture-review-9-abcdef012345",
        "reviewed_at": "2026-09-21T22:00:00+00:00",
        "question": REVIEW_QUESTION,
        "project": "Project",
        "source_root": "src/architecture_assistant",
        "check_source_root": "src/architecture_assistant",
        "source_root_verified": True,
        "step_no": 9,
        "architecture_version": "1.1",
        "deterministic_gate": {
            "available": True,
            "reason": "",
            "compliant": True,
            "baseline_version": "1.1",
            "rules": ["unknown-layer"],
            "violation_count": 0,
            "finding_ids": [],
            "decision_id": "realization-9-1",
            "decision_status": "ACCEPTED",
        },
        "providers": [
            {
                "source": "openai",
                "status": "FINDING",
                "reason": "",
                "finding": {
                    "id": "finding-openai-9-abc123abc123",
                    "source": "openai",
                    "claim": "the module boundary is clear",
                    "evidence": ["application/context.py:12"],
                    "confidence": 0.6,
                    "severity": "MEDIUM",
                    "step_no": 9,
                },
                "relation": "UNRESOLVED",
                "relation_target": None,
                "cost": {
                    "available": True,
                    "reason": "",
                    "total_usd": 0.01,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "record_count": 1,
                    "priced_record_count": 1,
                    "unpriced_record_count": 0,
                },
            },
            {
                "source": "claude",
                "status": "ABSTAIN",
                "reason": "advisor abstained: ClaudeAdvisorAbstainError",
                "finding": None,
                "relation": "UNRESOLVED",
                "relation_target": None,
                "cost": {"available": False, "reason": "no record"},
            },
            {
                "source": "grok",
                "status": "ERROR",
                "reason": "advisor failed: GrokMissingApiKeyError",
                "finding": None,
                "relation": "UNRESOLVED",
                "relation_target": None,
                "cost": {"available": False, "reason": "no record"},
            },
        ],
        "evidence": {
            "findings": [{"id": "finding-openai-9-abc123abc123"}],
            "supporting_ids": [],
            "conflicting_ids": [],
            "unresolved_ids": ["finding-openai-9-abc123abc123"],
            "abstained": [{"source": "claude", "reason": "advisor abstained"}],
            "errors": [{"source": "grok", "reason": "advisor failed"}],
            "conflicts": [],
            "unresolved_questions": [],
            "gate_status": "ACCEPTED",
            "gate_anchors": ["unknown-layer"],
        },
        "conflicts": [],
        "judge": {
            "available": True,
            "consulted": False,
            "status": "NOT_CONSULTED",
            "reason": "no evidence conflict was merged",
            "count": 0,
            "judgments": [],
        },
        "decision": {
            "id": "advisor-decision-9-abc123",
            "status": "ACCEPTED",
            "decision": "accepted: the implementation satisfies the checks",
            "rationale": "The deterministic architecture gate accepted it.",
            "perspectives": ["openai"],
            "evidence_refs": [],
        },
        "cost": {
            "providers": [
                {"source": "openai", "available": True, "total_usd": 0.01},
                {"source": "claude", "available": False, "reason": "no record"},
                {"source": "grok", "available": False, "reason": "no record"},
            ],
            "judge": {"available": False, "reason": "the judge was not consulted"},
            "total": {"available": True, "total_usd": 0.01},
        },
        "persistence": {
            "written": False,
            "reason": "the review is advisory and read-only: nothing is persisted",
        },
    }
    payload.update(overrides)
    return payload


def make_proposal(**overrides: Any) -> dict[str, Any]:
    """One managed-project proposal as the core would report it, plain data."""
    proposal: dict[str, Any] = {
        "proposal_id": "proposal-1",
        "project": "youtube_to_mp3",
        "created_at": "2026-09-21T22:00:00+00:00",
        "requirement": "MP3 -> TXT. Windows Python GUI.",
        "summary": "Managed-project architecture proposal",
        "source_review_id": "architecture-review-9-abcdef012345",
        "fingerprint": "a" * 64,
        "modules": [
            {
                "name": "gui",
                "responsibility": "the Windows panel",
                "dependencies": "application",
                "boundary_notes": "no download logic here",
            }
        ],
        "data_flows": [
            {"from": "cli", "to": "domain", "description": "one call"}
        ],
        "external_dependencies": [
            {"name": "ffmpeg", "purpose": "audio", "impact": "external binary"}
        ],
        "architecture_rules": [],
        "proposed_rules": ["one responsibility per module"],
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
                "rationale": "easy to test",
                "recommended_status": "PROPOSED",
            }
        ],
        "implementation_phases": [
            {"phase": "ARCHITECTURE", "goal": "lay it out", "scope": "one week"}
        ],
        "unresolved_questions": ["Which audio format?"],
        "rationale": "Assembled deterministically from the review.",
        "review_digest": {
            "schema_version": "1.0",
            "review_id": "architecture-review-9-abcdef012345",
            "provider_count": 3,
            "finding_count": 1,
            "abstain_count": 1,
            "error_count": 1,
            "finding_sources": ["openai"],
            "conflicts": [],
            "judge": {"status": "NOT_CONSULTED"},
            "decision": {"status": "PENDING"},
            "deterministic_gate": {
                "available": True,
                "compliant": True,
                "violation_count": 0,
                "baseline_version": "1.1",
            },
        },
        "architecture_version": "1.1",
        "revision_no": 1,
        "revision_of": None,
        "status": "DRAFT",
        "decided_by": "",
        "decided_at": None,
        "decision_reason": "",
        "revision_feedback": "",
        "superseded_by": None,
    }
    proposal.update(overrides)
    return proposal


_NO_LATEST = object()


def make_board(
    *,
    review_available: bool = True,
    latest: Any = _NO_LATEST,
    count: int = 1,
) -> dict[str, Any]:
    """One proposal board as the core reports it (plain data only)."""
    if latest is _NO_LATEST:
        proposal: Any = make_proposal()
    else:
        proposal = latest
    return {
        "review_available": review_available,
        "synthesizer": False,
        "count": count,
        "proposals": [] if proposal is None else [proposal],
        "latest": proposal,
    }


_NO_RECORD = object()


def make_supervision_record(**overrides: Any) -> dict[str, Any]:
    """One supervision record, exactly as the core's payload renders it."""
    record: dict[str, Any] = {
        "supervision_id": "a" * 64,
        "project": "Project",
        "step_no": 9,
        "attempt": 1,
        "source_report_hash": "b" * 64,
        "architecture_version": "1.1",
        "status": "WAITING_HUMAN",
        "action": "CLARIFY",
        "risk": "LOW",
        "reason": "the report is missing its test command",
        "evidence": ["application/context.py:12"],
        "instruction_for_cline": "Add the pytest command to the report.",
        "requires_human": True,
        "provider": "scripted",
        "cost_available": False,
        "decided_by": "",
        "decided_at": None,
        "decision_reason": "",
        "first_seen_at": "2026-09-21T22:00:00+00:00",
        "created_at": "2026-09-21T22:00:00+00:00",
        "updated_at": "2026-09-21T22:00:00+00:00",
        "sent_at": None,
        "escalated_at": None,
    }
    record.update(overrides)
    return record


def make_supervisor_context(**overrides: Any) -> dict[str, Any]:
    """The bounded fact sheet a supervisor would be handed (plain data)."""
    context: dict[str, Any] = {
        "project": "Project",
        "step_no": 9,
        "attempt": 1,
        "max_attempts": 3,
        "attempts_remaining": 2,
        "step_state": "REPORT_RECEIVED",
        "mode": "MANUAL",
        "paused": False,
        "architecture_version": "1.1",
        "source_report_hash": "b" * 64,
        "task_title": "GUI step",
        "task_description": "do the thing",
        "task_instructions": {},
        "report_status": "DONE",
        "report_summary": "the step is done",
        "report": {"summary": "the step is done"},
        "report_files_created": ["src/new.py"],
        "report_files_changed": [],
        "report_files_deleted": [],
        "report_tests": {"passed": 3, "failed": 0},
        "worker_issues": ["a note"],
        "worker_architecture_questions": [],
        "worker_dependencies_added": [],
        "deterministic_findings": [{"id": "finding-1", "claim": "no violation"}],
        "accepted_adrs": [{"id": "ADR-001", "title": "sqlite only"}],
        "open_risks": [{"id": "risk-001", "description": "pilot risk"}],
        "open_change_requests": [{"request_id": "ACR-001", "title": "add monitor"}],
        "operator_constraints": ["keep it Python-only"],
    }
    context.update(overrides)
    return context


def make_supervisor_status(
    *,
    enabled: bool = True,
    record: Any = _NO_RECORD,
    waiting_for: str = "human-decision",
    allowed: bool = False,
    verdict_reason: str = "supervision-waiting_human",
    report_hash: Optional[str] = "b" * 64,
    context: Optional[dict[str, Any]] = None,
    history: Optional[list[Any]] = None,
    runtime: Optional[dict[str, Any]] = None,
    reason: str = "",
    payload_available: bool = True,
) -> dict[str, Any]:
    """One supervision board as the core reports it (plain data only)."""
    current: Any = make_supervision_record() if record is _NO_RECORD else record
    payload: dict[str, Any] = {
        "enabled": enabled,
        "project": "Project",
        "step_no": 9,
        "attempt": 1,
        "step_state": "REPORT_RECEIVED",
        "max_attempts": 3,
        "attempts_remaining": 2,
        "mode": "MANUAL",
        "paused": False,
        "architecture_version": "1.1",
        "current_report_hash": report_hash,
        "waiting_for": waiting_for,
        "verdict": {
            "allowed": allowed,
            "status": None if current is None else current.get("status"),
            "supervision_id": (
                None if current is None else current.get("supervision_id")
            ),
            "reason": verdict_reason,
            "source_report_hash": report_hash,
        },
        "record": current,
        "history": list(
            history or ([] if current is None else [current])
        ),
        "context": (
            context if context is not None else make_supervisor_context()
        ),
        "provider": "scripted",
        "malformed_stability_seconds": 30.0,
        "malformed_timeout_seconds": 120.0,
    }
    return {
        "enabled": enabled,
        "runtime": (
            runtime
            if runtime is not None
            else {"state": "RUNNING", "running": True}
        ),
        "supervision": payload if payload_available else None,
        "reason": reason,
    }


class FakeRunner:
    """Records jobs instead of running them, like the real runner would."""

    def __init__(self, *, busy: bool = False) -> None:
        self.jobs: list[tuple[str, Any]] = []
        self.busy = busy
        self.started = False

    def start(self) -> None:
        self.started = True

    @property
    def is_busy(self) -> bool:
        return self.busy

    def submit(self, label: str, action: Any) -> bool:
        if self.busy:
            return False
        self.jobs.append((label, action))
        return True

    def run_next(self, worker: Any) -> JobResult:
        """Run the next recorded job against ``worker``, like the core thread."""
        label, action = self.jobs.pop(0)
        try:
            payload = action(worker)
        except Exception as error:  # mirrors the real runner exactly
            return JobResult(label=label, error=describe_error(error))
        return JobResult(label=label, payload=payload)


class RecordingWorker:
    """A fake core worker: records every call and answers with plain data."""

    def __init__(
        self,
        *,
        payload: Optional[dict[str, Any]] = None,
        preview: Optional[dict[str, Any]] = None,
        review: Optional[dict[str, Any]] = None,
        board: Optional[dict[str, Any]] = None,
        supervisor: Optional[dict[str, Any]] = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._payload = payload if payload is not None else make_payload()
        self._preview = dict(preview) if preview is not None else make_preview()
        self._review = dict(review) if review is not None else make_review()
        self._board = dict(board) if board is not None else make_board()
        self._supervisor = (
            dict(supervisor)
            if supervisor is not None
            else make_supervisor_status(
                enabled=False,
                payload_available=False,
                reason="there is no current step to supervise",
                runtime={"state": "STOPPED", "running": False},
            )
        )

    def _record(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, kwargs))
        return {"action": name, **kwargs}

    @property
    def call_names(self) -> list[str]:
        return [name for name, _kwargs in self.calls]

    def payload(self) -> dict[str, Any]:
        self.calls.append(("payload", {}))
        return self._payload

    def audit_tail(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        self.calls.append(("audit_tail", {"limit": limit}))
        return [{"created_at": "2026-09-21T22:00:00", "detail": "{}"}]

    def proposal_board(self) -> dict[str, Any]:
        self.calls.append(("proposal_board", {}))
        return self._board

    def supervisor_status(self) -> dict[str, Any]:
        self.calls.append(("supervisor_status", {}))
        return self._supervisor

    def analyze_report(self) -> dict[str, Any]:
        self.calls.append(("analyze_report", {}))
        return {
            "action": "analyze_report",
            **self._supervision_fresh(),
            "analysis": {
                "outcome": "analyzed",
                "step_no": 9,
                "attempt": 1,
                "status": "WAITING_HUMAN",
                "supervision_id": "a" * 64,
                "source_report_hash": "b" * 64,
                "action": "CLARIFY",
                "provider_called": True,
                "gate_allowed": False,
                "waiting_for": "human-decision",
                "reason": "the report is missing its test command",
            },
        }

    def approve_and_send(
        self, instruction: str = "", *, actor: str, reason: str
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "approve_and_send",
                {"instruction": instruction, "actor": actor, "reason": reason},
            )
        )
        return {
            "action": "approve_and_send",
            "instruction": instruction,
            "actor": actor,
            "reason": reason,
            **self._supervision_fresh(),
            "record": make_supervision_record(
                status="SENT",
                decided_by=actor,
                decision_reason=reason,
                instruction_for_cline=instruction,
            ),
        }

    def reject_directive(self, *, actor: str, reason: str) -> dict[str, Any]:
        self.calls.append(
            ("reject_directive", {"actor": actor, "reason": reason})
        )
        return {
            "action": "reject_directive",
            "actor": actor,
            "reason": reason,
            **self._supervision_fresh(),
            "record": make_supervision_record(
                status="REJECTED", decided_by=actor, decision_reason=reason
            ),
        }

    def waive_supervision(self, *, actor: str, reason: str) -> dict[str, Any]:
        self.calls.append(
            ("waive_supervision", {"actor": actor, "reason": reason})
        )
        return {
            "action": "waive_supervision",
            "actor": actor,
            "reason": reason,
            **self._supervision_fresh(),
            "record": make_supervision_record(
                status="WAIVED", decided_by=actor, decision_reason=reason
            ),
        }

    def escalate_supervision(self, *, actor: str, reason: str) -> dict[str, Any]:
        self.calls.append(
            ("escalate_supervision", {"actor": actor, "reason": reason})
        )
        return {
            "action": "escalate_supervision",
            "actor": actor,
            "reason": reason,
            **self._supervision_fresh(),
            "record": make_supervision_record(
                status="ESCALATED", decided_by=actor, decision_reason=reason
            ),
        }

    def supervisor_tick(self) -> dict[str, Any]:
        return {
            "action": "supervisor_tick",
            "result": {
                "state": "RUNNING",
                "outcome": "unchanged",
                "waiting_for": "human-decision",
            },
            "supervisor": self._supervisor,
        }

    def _supervision_fresh(self) -> dict[str, Any]:
        """The fresh read every supervision action returns, exactly like the core."""
        return {
            "payload": self._payload,
            "audit": self.audit_tail(),
            "supervisor": self._supervisor,
            "step_no": 9,
            "attempt": 1,
        }

    def synthesize_proposal(
        self,
        requirement: str = "",
        *,
        actor: str,
        reason: str,
        revision_of: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._record(
            "synthesize_proposal",
            requirement=requirement,
            actor=actor,
            reason=reason,
            revision_of=revision_of,
            proposal_id="proposal-1",
            status="DRAFT",
        )

    def approve_proposal(
        self, proposal_id: str, *, actor: str, reason: str
    ) -> dict[str, Any]:
        return self._record(
            "approve_proposal",
            proposal_id=proposal_id,
            actor=actor,
            reason=reason,
            status="APPROVED",
        )

    def reject_proposal(
        self, proposal_id: str, *, actor: str, reason: str
    ) -> dict[str, Any]:
        return self._record(
            "reject_proposal",
            proposal_id=proposal_id,
            actor=actor,
            reason=reason,
            status="REJECTED",
        )

    def request_proposal_revision(
        self,
        proposal_id: str,
        *,
        actor: str,
        reason: str,
        feedback: str,
    ) -> dict[str, Any]:
        return self._record(
            "request_proposal_revision",
            proposal_id=proposal_id,
            actor=actor,
            reason=reason,
            feedback=feedback,
            status="REVISION_REQUESTED",
        )

    def run_until_idle(self) -> dict[str, Any]:
        return self._record(
            "run_until_idle", stopped_because="worker-report-pending"
        )

    def pause_project(self, *, actor: str, reason: str) -> dict[str, Any]:
        return self._record(
            "pause_project", actor=actor, reason=reason, paused=True
        )

    def resume_project(self, *, actor: str, reason: str) -> dict[str, Any]:
        return self._record(
            "resume_project", actor=actor, reason=reason, paused=False
        )

    def approve(self, *, actor: str, reason: str) -> dict[str, Any]:
        return self._record(
            "approve", actor=actor, reason=reason, step_no=9, state="DISPATCHED"
        )

    def reject(self, *, actor: str, reason: str) -> dict[str, Any]:
        return self._record(
            "reject", actor=actor, reason=reason, step_no=9, state="READY"
        )

    def unblock(self, *, actor: str, reason: str) -> dict[str, Any]:
        return self._record(
            "unblock", actor=actor, reason=reason, step_no=9, state="READY"
        )

    def resolve(self, *, actor: str, reason: str) -> dict[str, Any]:
        return self._record(
            "resolve", actor=actor, reason=reason, step_no=9, state="READY"
        )

    def abort(self, *, actor: str, reason: str) -> dict[str, Any]:
        return self._record(
            "abort", actor=actor, reason=reason, step_no=9, state="ABORTED"
        )

    def export_markdown(self) -> dict[str, Any]:
        return self._record("export_markdown", path="reports/x.md")

    def export_excel(self) -> dict[str, Any]:
        return self._record("export_excel", path="reports/x.xlsx")

    def reconnect(self) -> None:
        self.calls.append(("reconnect", {}))

    def run_architecture_review(self, question: str) -> dict[str, Any]:
        self.calls.append(("run_architecture_review", {"question": question}))
        return dict(self._review)

    def preview_plan(self, text: str, *, source_file: str = "") -> dict[str, Any]:
        self.calls.append(
            ("preview_plan", {"text": text, "source_file": source_file})
        )
        # Echo the reported file, exactly like CoreWorker.preview_plan does.
        preview = dict(self._preview)
        preview["source_file"] = source_file or preview.get("source_file", "")
        return preview

    def import_plan(
        self,
        text: str,
        *,
        actor: str,
        reason: str,
        source_file: str = "",
    ) -> dict[str, Any]:
        return self._record(
            "import_plan",
            text=text,
            actor=actor,
            reason=reason,
            source_file=source_file,
            project_name="Project",
            plan_version="0.3",
            plan_hash="a" * 64,
            step_count=2,
            first_step_no=1,
            last_step_no=2,
        )


class ImportingWorker(RecordingWorker):
    """A worker whose projection gains the imported plan, like the real one."""

    def import_plan(
        self,
        text: str,
        *,
        actor: str,
        reason: str,
        source_file: str = "",
    ) -> dict[str, Any]:
        self._payload = make_payload(state="PENDING")
        return super().import_plan(
            text, actor=actor, reason=reason, source_file=source_file
        )


def apply_payload(controller: GuiController, **kwargs: Any) -> None:
    """Feed the controller one finished refresh, as the pump would."""
    payload = make_payload(**kwargs)
    controller.apply_result(
        JobResult(label="refresh", payload={"payload": payload, "audit": []})
    )


def make_controller(
    *,
    actor: str = "operator",
    runner: Optional[FakeRunner] = None,
    **payload: Any,
) -> GuiController:
    """A controller showing one payload, with a recording runner."""
    controller = GuiController(
        runner if runner is not None else FakeRunner(),
        actor=actor,
        reports_dir="reports",
    )
    apply_payload(controller, **payload)
    return controller


class TestButtonMatrixMatchesTheFsm:
    """The GUI advertises exactly the transitions the core allows."""

    def test_approve_and_reject_match_the_transition_table(self) -> None:
        assert APPROVAL_STATES == _states_for(StepEvent.APPROVE)
        assert APPROVAL_STATES == _states_for(StepEvent.REJECT)

    def test_unblock_resolve_and_abort_match_the_transition_table(self) -> None:
        assert UNBLOCK_STATES == _states_for(StepEvent.UNBLOCK)
        assert RESOLVE_STATES == _states_for(StepEvent.RESOLVE)
        assert ABORT_STATES == _states_for(StepEvent.ABORT)

    def test_abort_is_never_advertised_from_a_state_that_forbids_it(self) -> None:
        for state in ("READY", "DISPATCHED", "CLINE_WORKING", "REVISE"):
            assert state not in ABORT_STATES, state


def _named(name: str) -> Exception:
    """An exception whose class *name* is ``name`` (classification is by name)."""
    return type(name, (Exception,), {})(f"{name} raised for the test")


class TestEnableMatrix:
    """Buttons follow the persisted state - as advice, never as authority."""

    @pytest.mark.parametrize("state", [state.value for state in StepState])
    def test_step_buttons_follow_the_state(self, state) -> None:
        controller = make_controller(state=state)
        enabled = lambda key: controller.enabled(controller.intent(key))

        assert enabled("approve") is (state == "WAITING_APPROVAL")
        assert enabled("reject") is (state == "WAITING_APPROVAL")
        assert enabled("unblock") is (state == "BLOCKED")
        assert enabled("resolve") is (state == "CONFLICT")
        assert enabled("abort") is (state in ABORT_STATES)

    def test_no_steps_disables_the_step_actions(self) -> None:
        controller = make_controller()

        for key in (
            "approve",
            "reject",
            "unblock",
            "resolve",
            "abort",
            "run_until_idle",
        ):
            assert controller.enabled(controller.intent(key)) is False, key
        assert controller.view_model()["work"]["no_steps"] is True

    def test_pause_and_resume_follow_the_project_flag(self) -> None:
        running = make_controller(paused=False)
        assert running.enabled(running.intent("pause_project")) is True
        assert running.enabled(running.intent("resume_project")) is False

        paused = make_controller(paused=True, state="READY")
        assert paused.enabled(paused.intent("pause_project")) is False
        assert paused.enabled(paused.intent("resume_project")) is True
        assert paused.enabled(paused.intent("run_until_idle")) is False

    def test_reports_and_diagnostics_stay_available(self) -> None:
        controller = make_controller(mode="MANUAL")

        for key in (
            "export_markdown",
            "export_excel",
            "open_reports_folder",
            "refresh",
            "view_snapshot",
            "reconnect",
        ):
            assert controller.enabled(controller.intent(key)) is True, key

    def test_everything_submitting_is_disabled_while_an_action_runs(
        self,
    ) -> None:
        runner = FakeRunner()
        controller = make_controller(
            runner=runner, state="WAITING_APPROVAL"
        )

        assert controller.submit("approve", reason="reviewed") is True
        assert controller.is_busy is True

        for intent in INTENTS:
            expected = intent.key == "view_snapshot"
            assert controller.enabled(intent) is expected, intent.key

        controller.apply_result(
            JobResult(
                label="approve",
                payload={"result": {"action": "approve", "step_no": 9}},
            )
        )

        assert controller.is_busy is False

    def test_a_critical_failure_disables_mutations_and_keeps_diagnostics(
        self,
    ) -> None:
        controller = make_controller(state="WAITING_APPROVAL")

        controller.apply_result(
            JobResult(
                label="run_until_idle",
                error=describe_error(_named("RealizationControlError")),
            )
        )

        assert controller.is_critical is True
        for key in (
            "run_until_idle",
            "approve",
            "reject",
            "unblock",
            "resolve",
            "abort",
            "export_markdown",
            "export_excel",
        ):
            assert controller.enabled(controller.intent(key)) is False, key
        for key in ("refresh", "reconnect", "view_snapshot"):
            assert controller.enabled(controller.intent(key)) is True, key
        assert controller.view_model()["banner"]["critical"] is True
        assert "CRITICAL" in controller.view_model()["banner"]["text"]

    def test_a_successful_refresh_clears_the_critical_state(self) -> None:
        controller = make_controller(state="READY")
        controller.apply_result(
            JobResult(
                label="run_until_idle",
                error=describe_error(_named("ProgrammingError")),
            )
        )
        assert controller.is_critical is True

        apply_payload(controller, state="READY")

        assert controller.is_critical is False
        assert controller.last_error is None
        assert controller.view_model()["banner"]["critical"] is False


class TestHumanContext:
    """An actor and a per-action reason are required, and travel verbatim."""

    def test_an_actor_is_required(self) -> None:
        runner = FakeRunner()
        controller = make_controller(
            runner=runner, actor="", state="WAITING_APPROVAL"
        )

        assert controller.submit("approve", reason="reviewed") is False
        assert runner.jobs == []
        assert "actor is required" in controller.status

    def test_a_reason_is_required(self) -> None:
        runner = FakeRunner()
        controller = make_controller(
            runner=runner, state="WAITING_APPROVAL"
        )

        assert controller.submit("approve", reason="   ") is False
        assert runner.jobs == []
        assert "reason is required" in controller.status

    def test_the_reason_reaches_the_core_verbatim(self) -> None:
        runner = FakeRunner()
        controller = make_controller(runner=runner, state="WAITING_APPROVAL")

        assert controller.submit("approve", reason="  reviewed by hand  ")
        worker = RecordingWorker()
        result = runner.run_next(worker)

        assert result.ok is True
        name, arguments = worker.calls[0]
        assert name == "approve"
        assert arguments["actor"] == "operator"
        assert arguments["reason"] == "reviewed by hand"

    def test_a_non_human_action_needs_no_reason(self) -> None:
        runner = FakeRunner()
        controller = make_controller(runner=runner, state="READY")

        assert controller.submit("run_until_idle") is True
        assert runner.jobs[0][0] == "run_until_idle"

    @pytest.mark.parametrize(
        "key, state, expected",
        [
            ("run_until_idle", "READY", "run_until_idle"),
            ("pause_project", "READY", "pause_project"),
            ("resume_project", "READY", "resume_project"),
            ("approve", "WAITING_APPROVAL", "approve"),
            ("reject", "WAITING_APPROVAL", "reject"),
            ("unblock", "BLOCKED", "unblock"),
            ("resolve", "CONFLICT", "resolve"),
            ("abort", "BLOCKED", "abort"),
            ("export_markdown", "READY", "export_markdown"),
            ("export_excel", "READY", "export_excel"),
            ("refresh", "READY", "payload"),
            ("reconnect", "READY", "reconnect"),
        ],
    )
    def test_each_action_makes_exactly_the_core_call_it_promises(
        self, key, state, expected
    ) -> None:
        runner = FakeRunner()
        controller = make_controller(
            runner=runner, state=state, paused=key == "resume_project"
        )

        assert controller.submit(key, reason="operator reason") is True
        worker = RecordingWorker()
        result = runner.run_next(worker)

        assert result.ok is True
        assert worker.call_names.count(expected) == 1
        # Every action is followed by a fresh read of the projection + audit.
        assert "payload" in worker.call_names
        assert "audit_tail" in worker.call_names


class TestErrorHandling:
    """A failure is reported and never changes the view locally."""

    def test_a_failed_action_keeps_the_last_payload(self) -> None:
        controller = make_controller(state="WAITING_APPROVAL")
        before = controller.view_model()["work"]

        controller.apply_result(
            JobResult(
                label="approve",
                error=describe_error(_named("ApprovalTransitionError")),
            )
        )

        assert controller.view_model()["work"] == before
        assert controller.is_critical is False
        assert "ApprovalTransitionError" in controller.status
        assert controller.last_error is not None
        assert "Last action failed" in controller.view_model()["banner"]["text"]

    def test_an_error_never_raises_out_of_apply_result(self) -> None:
        controller = make_controller(state="READY")

        controller.apply_result(
            JobResult(
                label="refresh",
                error=describe_error(_named("OperationalError")),
            )
        )

        assert controller.status.startswith("refresh failed")
        assert controller.is_critical is True


class TestViewModel:
    """The view model is plain data, complete and honest about what it shows."""

    def test_the_button_map_covers_every_intent(self) -> None:
        controller = make_controller(state="READY")
        buttons = controller.view_model()["buttons"]

        assert set(buttons) == {intent.key for intent in INTENTS}
        assert all("enabled" in spec for spec in buttons.values())
        assert all(spec["label"] for spec in buttons.values())

    def test_the_top_and_work_panels_reflect_the_payload(self) -> None:
        controller = make_controller(
            state="CLINE_WORKING",
            attempt=2,
            tasks=[
                {
                    "step_no": 9,
                    "attempt": 2,
                    "state": "DISPATCHED",
                    "report_status": None,
                }
            ],
        )
        view = controller.view_model()

        assert view["top"]["mode"] == "AUTO"
        assert view["top"]["project_state"] == "running"
        assert view["top"]["architecture"] == "1.1"
        assert view["work"]["step_no"] == "9"
        assert view["work"]["state"] == "CLINE_WORKING"
        assert view["work"]["attempt"] == "2/3"
        assert view["work"]["channel"] == "DISPATCHED / report -"
        assert view["work"]["no_steps"] is False
        assert view["reports_dir"] == "reports"

    def test_an_empty_plan_says_so_and_offers_no_plan_generation(self) -> None:
        controller = make_controller()
        view = controller.view_model()

        assert view["work"]["no_steps"] is True
        assert view["work"]["step_no"] == "-"

    def test_the_monitor_rows_and_snapshot_come_from_the_projection(self) -> None:
        controller = make_controller(state="READY")

        rows = dict(controller.monitor_rows())
        assert rows["Mode"] == "AUTO"
        assert rows["Architecture version"] == "1.1"
        assert rows["Steps"] == "1"
        assert rows["Current state"] == "READY"
        assert rows["Blocking steps"] == "-"
        assert json.loads(controller.snapshot_json())["schema_version"] == "1.0"

    def test_the_blocking_and_risk_rows_are_read_only_rows(self) -> None:
        controller = make_controller(state="BLOCKED")

        assert dict(controller.monitor_rows())["Blocking steps"] == "9:BLOCKED"

    def test_the_audit_rows_are_plain_read_only_rows(self) -> None:
        controller = make_controller(state="READY")
        controller.apply_result(
            JobResult(
                label="refresh",
                payload={
                    "payload": make_payload(state="READY"),
                    "audit": [
                        {
                            "created_at": "2026-09-21T22:00:00+00:00",
                            "entity_type": "STEP",
                            "entity_id": "9",
                            "action": "UPDATE",
                            "event": "APPROVE",
                            "actor": "alice",
                            "step_no": 9,
                            "detail": '{"a": 1}',
                        }
                    ],
                },
            )
        )

        row = controller.audit_rows()[0]

        assert row[0] == "2026-09-21 22:00:00"
        assert row[1] == "STEP 9"
        assert row[2] == "UPDATE"
        assert row[3] == "APPROVE"
        assert row[4] == "alice"
        assert row[5] == "9"

    def test_the_stopped_because_comes_from_the_last_run(self) -> None:
        runner = FakeRunner()
        controller = make_controller(runner=runner, state="READY")

        assert controller.stopped_because == ""

        assert controller.submit("run_until_idle") is True
        controller.apply_result(runner.run_next(RecordingWorker()))

        assert controller.stopped_because == "worker-report-pending"
        assert (
            controller.view_model()["work"]["stopped_because"]
            == "worker-report-pending"
        )
        assert controller.view_model()["status"].startswith("Loop stopped")

    def test_the_log_is_bounded(self) -> None:
        controller = make_controller(state="READY")

        for index in range(400):
            controller.log(f"line {index}")

        lines = controller.log_lines()
        assert len(lines) == controller.log_limit == 200
        # the oldest kept line is the 200th, and the newest is the last
        assert lines[0].endswith("GUI | note | line 200")
        assert lines[-1].endswith("GUI | note | line 399")
        assert controller.log_count == 200
        # the rows are the same entries, newest first
        rows = controller.log_rows()
        assert rows[0][5] == "line 399"
        assert rows[-1][5] == "line 200"
        assert controller.view_model()["log"]["lines"][-1].endswith(
            "GUI | note | line 399"
        )

    def test_only_the_documented_intents_exist(self) -> None:
        controller = make_controller()

        with pytest.raises(ValueError):
            controller.intent("verify_step")
        with pytest.raises(ValueError):
            controller.submit("run_anything", reason="no")

    def test_a_controller_needs_a_runner(self) -> None:
        with pytest.raises(ValueError):
            GuiController(object())
        with pytest.raises(ValueError):
            GuiController(FakeRunner(), log_limit=0)


def _critical_error() -> BaseException:
    """An error the worker classifies as CRITICAL, by class name."""

    class ProjectMissingError(Exception):
        pass

    return ProjectMissingError("the source of truth contains no project")


def _conflict_error() -> BaseException:
    """The operator-fixable failure a second import produces."""

    class PlanConflictError(Exception):
        pass

    return PlanConflictError("plan already loaded: 2 step(s) exist")


def plan_controller(
    *,
    preview: Optional[dict[str, Any]] = None,
    worker: Optional[RecordingWorker] = None,
    **payload: Any,
) -> tuple[FakeRunner, GuiController, RecordingWorker]:
    """A controller with a plan loaded, previewed and awaiting a decision."""
    runner = FakeRunner()
    controller = make_controller(runner=runner, **payload)
    controller.set_plan(PLAN_TEXT, source_file="C:/plans/plan.json")
    assert controller.submit("load_plan") is True
    core = worker if worker is not None else RecordingWorker(preview=preview)
    controller.apply_result(runner.run_next(core))
    return runner, controller, core


class TestPlanLoader:
    """Load Plan / Import Plan: preview first, then one confirmed core call."""

    def test_load_plan_is_disabled_when_steps_exist(self) -> None:
        controller = make_controller(state="READY")

        assert controller.enabled(controller.intent("load_plan")) is False
        assert controller.submit("load_plan") is False
        assert "Plan already loaded" in controller.status
        assert controller.plan_pending is False

    def test_load_plan_is_available_on_an_empty_plan(self) -> None:
        controller = make_controller()

        assert controller.enabled(controller.intent("load_plan")) is True
        assert controller.view_model()["work"]["plan"] == "no plan loaded"

    def test_load_plan_needs_the_core_to_be_reachable(self) -> None:
        controller = make_controller()
        controller.apply_result(
            JobResult(label="refresh", error=describe_error(_critical_error()))
        )

        assert controller.is_critical is True
        assert controller.enabled(controller.intent("load_plan")) is False

    def test_the_preview_lines_carry_the_project_and_the_step_count(self) -> None:
        _, controller, _ = plan_controller()

        text = "\n".join(controller.plan_preview_lines())

        assert "C:/plans/plan.json" in text
        assert "Project" in text
        assert "Steps:             2  (1 ... 2)" in text
        assert "Risk:              LOW 1, MEDIUM 1, HIGH 0" in text
        assert "Requires a human:  1" in text
        assert "CONTEXT, ARCHITECTURE" in text
        assert "Ready to import" in text

    def test_the_preview_reports_a_blocked_import_with_its_reason(self) -> None:
        _, controller, _ = plan_controller(
            preview=make_preview(
                importable=False,
                blocked_reason="the database already holds 2 step(s)",
                db_step_count=2,
            )
        )

        text = "\n".join(controller.plan_preview_lines())

        assert "Cannot import: the database already holds 2 step(s)" in text
        assert controller.plan_importable() is False
        assert controller.enabled(controller.intent("import_plan")) is False

    def test_an_invalid_plan_lists_every_issue(self) -> None:
        _, controller, _ = plan_controller(
            preview=make_preview(
                valid=False,
                importable=False,
                blocked_reason="the plan file is invalid",
                step_count=0,
                first_step_no=None,
                last_step_no=None,
                issues=[
                    {
                        "path": "steps[0].phase",
                        "problem": "must be one of (CONTEXT, ...)",
                        "value": "'ANALYSIS'",
                        "message": "steps[0].phase must be one of (CONTEXT, ...)",
                    },
                    {
                        "path": "steps[1].max_attempts",
                        "problem": "must be an integer >= 1",
                        "value": "0",
                        "message": "steps[1].max_attempts must be >= 1",
                    },
                ],
            )
        )

        text = "\n".join(controller.plan_preview_lines())

        assert "2 validation problem(s):" in text
        assert "steps[0].phase" in text
        assert "'ANALYSIS'" in text
        assert "steps[1].max_attempts" in text

    def test_a_preview_is_needed_before_an_import(self) -> None:
        controller = make_controller()
        controller.set_plan(PLAN_TEXT, source_file="C:/plans/plan.json")

        assert controller.enabled(controller.intent("import_plan")) is False
        assert controller.submit("import_plan", reason="because") is False
        assert "cannot be imported" in controller.status

    def test_import_plan_without_a_plan_says_so(self) -> None:
        controller = make_controller()

        assert controller.submit("import_plan", reason="because") is False
        assert "Load a plan file first." in controller.status

    def test_cancel_clears_the_plan_without_a_core_request(self) -> None:
        runner, controller, _ = plan_controller()
        runner.jobs.clear()

        controller.clear_plan()

        assert controller.plan_pending is False
        assert controller.plan_preview is None
        assert controller.plan_importable() is False
        assert controller.plan_preview_lines() == ["No plan is loaded."]
        assert runner.jobs == []
        assert controller.view_model()["work"]["plan"] == "no plan loaded"

    def test_confirm_submits_exactly_one_import_request(self) -> None:
        runner, controller, core = plan_controller()
        runner.jobs.clear()

        assert controller.submit("import_plan", reason="the approved plan")
        assert [label for label, _action in runner.jobs] == ["import_plan"]

        assert controller.submit("import_plan", reason="again") is False
        assert len(runner.jobs) == 1

        controller.apply_result(runner.run_next(core))

        assert core.call_names.count("import_plan") == 1

    def test_the_import_request_carries_actor_reason_and_text(self) -> None:
        runner, controller, core = plan_controller()

        controller.submit("import_plan", reason="the approved plan")
        controller.apply_result(runner.run_next(core))

        calls = [call for call in core.calls if call[0] == "import_plan"]
        assert len(calls) == 1
        _name, arguments = calls[0]

        assert arguments["text"] == PLAN_TEXT
        assert arguments["actor"] == "operator"
        assert arguments["reason"] == "the approved plan"
        assert arguments["source_file"] == "C:/plans/plan.json"

    def test_a_successful_import_clears_the_pending_plan(self) -> None:
        runner, controller, core = plan_controller()

        controller.submit("import_plan", reason="the approved plan")
        controller.apply_result(runner.run_next(core))

        assert controller.plan_pending is False
        assert controller.plan_preview is None
        assert "Plan imported" in controller.status

    def test_a_failed_import_keeps_the_pending_plan(self) -> None:
        runner, controller, _core = plan_controller()

        controller.submit("import_plan", reason="the approved plan")
        controller.apply_result(
            JobResult(label="import_plan", error=describe_error(_conflict_error()))
        )

        assert controller.plan_pending is True
        assert controller.plan_importable() is True
        assert is_critical(_conflict_error()) is False
        assert "PlanConflictError" in controller.status

    def test_the_import_is_not_critical_when_it_is_only_a_plan_problem(
        self,
    ) -> None:
        from architecture_assistant.application import (
            PlanConflictError,
            PlanFormatError,
            PlanInvariantError,
            PlanIssue,
            PlanProjectMismatchError,
        )

        assert (
            is_critical(PlanFormatError([PlanIssue("a", "b", "c", "d")]))
            is False
        )
        assert is_critical(PlanConflictError("already loaded")) is False
        assert (
            is_critical(PlanProjectMismatchError("other project")) is False
        )
        assert is_critical(PlanInvariantError("no project")) is True

    def test_the_projection_after_an_import_shows_the_new_steps(self) -> None:
        runner, controller, core = plan_controller(worker=ImportingWorker())

        assert controller.step_count() == 0
        assert controller.enabled(controller.intent("run_until_idle")) is False

        controller.submit("import_plan", reason="the approved plan")
        controller.apply_result(runner.run_next(core))

        view = controller.view_model()
        assert controller.step_count() == 1
        assert view["work"]["plan"] == "1 step(s) loaded"
        assert view["work"]["step_no"] == "9"
        assert view["work"]["state"] == "PENDING"
        assert view["work"]["no_steps"] is False
        assert controller.enabled(controller.intent("run_until_idle")) is True
        assert controller.enabled(controller.intent("load_plan")) is False

    def test_a_new_file_invalidates_the_previous_preview(self) -> None:
        _, controller, _ = plan_controller()
        assert controller.plan_importable() is True

        controller.set_plan("{}", source_file="C:/plans/other.json")

        assert controller.plan_pending is True
        assert controller.plan_preview is None
        assert controller.plan_importable() is False
        assert (
            controller.plan_blocked_reason()
            == "the plan has not been validated yet"
        )
        assert controller.plan_preview_lines() == [
            "Source file: C:/plans/other.json",
            "The plan has not been validated yet.",
        ]

    def test_set_plan_refuses_anything_but_text(self) -> None:
        controller = make_controller()

        with pytest.raises(ValueError):
            controller.set_plan({"steps": []})  # type: ignore[arg-type]

    def test_the_plan_preview_is_a_copy(self) -> None:
        _, controller, _ = plan_controller()

        first = controller.plan_preview
        assert first is not None
        first["importable"] = False

        assert controller.plan_preview["importable"] is True


def review_controller(
    *,
    review: Optional[dict[str, Any]] = None,
    question: str = REVIEW_QUESTION,
    **payload: Any,
) -> tuple[FakeRunner, GuiController, RecordingWorker]:
    """A controller whose review question is set and whose core is reachable."""
    runner = FakeRunner()
    controller = make_controller(runner=runner, **payload)
    controller.set_review_question(question)
    return runner, controller, RecordingWorker(review=review)


class TestArchitectureReview:
    """Run Architecture Review: one question, one core call, plain data back."""

    def test_run_review_is_enabled_when_the_core_is_reachable(self) -> None:
        _runner, controller, _core = review_controller()

        assert controller.enabled(controller.intent("run_review")) is True
        assert controller.view_model()["review"]["available"] is False
        assert controller.view_model()["review"]["question"] == REVIEW_QUESTION

    def test_run_review_is_disabled_while_another_action_runs(self) -> None:
        runner, controller, _core = review_controller()
        runner.busy = True

        assert controller.enabled(controller.intent("run_review")) is False
        assert controller.submit("run_review") is False
        assert runner.jobs == []

    def test_the_run_review_question_is_required(self) -> None:
        _runner, controller, _core = review_controller()

        controller.set_review_question("   ")
        assert controller.submit("run_review") is False
        assert "review question is required" in controller.status

        controller.set_review_question(f"  {REVIEW_QUESTION}  ")
        assert controller.review_question == f"  {REVIEW_QUESTION}  "

    def test_set_review_question_refuses_anything_but_text(self) -> None:
        _runner, controller, _core = review_controller()

        with pytest.raises(ValueError):
            controller.set_review_question(None)  # type: ignore[arg-type]

    def test_the_review_is_one_core_call_with_the_operators_question(self) -> None:
        runner, controller, core = review_controller()
        runner.jobs.clear()

        assert controller.submit("run_review") is True
        assert [label for label, _action in runner.jobs] == ["run_review"]

        controller.apply_result(runner.run_next(core))

        assert core.call_names.count("run_architecture_review") == 1
        assert [
            call for call in core.calls if call[0] == "run_architecture_review"
        ] == [("run_architecture_review", {"question": REVIEW_QUESTION})]

    def test_the_review_payload_reaches_the_controller_as_plain_data(self) -> None:
        runner, controller, core = review_controller()

        controller.submit("run_review")
        controller.apply_result(runner.run_next(core))

        view = controller.view_model()["review"]
        assert view["available"] is True
        assert (
            json.loads(json.dumps(view))["provider_panels"][0]["source"]
            == "openai"
        )
        assert controller.review is not None
        assert (
            json.loads(json.dumps(controller.review))["judge"]["status"]
            == "NOT_CONSULTED"
        )
        assert "Review architecture-review-9" in controller.status
        assert controller.review is not core._review

    def test_the_review_header_shows_the_configured_source_root(self) -> None:
        runner, controller, core = review_controller()
        controller.submit("run_review")
        controller.apply_result(runner.run_next(core))

        rows = dict(controller.review_header())

        assert rows["Source root"].startswith("src/architecture_assistant")
        assert "confirmed by the realization check" in rows["Source root"]
        assert rows["Project"] == "Project"
        assert rows["Current step"] == "9"
        assert rows["Architecture version"] == "1.1"
        assert "compliant" in rows["Deterministic gate"]
        assert rows["Question"] == REVIEW_QUESTION
        assert rows["Persisted"] == "no - the review is advisory and read-only"

    def test_a_source_root_the_check_does_not_confirm_is_visible(self) -> None:
        reviewed = make_review(
            source_root="src/architecture_assistant",
            check_source_root="src/other_project",
            source_root_verified=False,
        )
        runner, controller, core = review_controller(review=reviewed)
        controller.submit("run_review")
        controller.apply_result(runner.run_next(core))

        rows = dict(controller.review_header())

        assert "NOT confirmed" in rows["Source root"]
        assert "src/other_project" in rows["Source root"]

    def test_three_advisor_panes_are_built_side_by_side(self) -> None:
        runner, controller, core = review_controller()
        controller.submit("run_review")
        controller.apply_result(runner.run_next(core))

        panels = controller.provider_panels()

        assert [panel["name"] for panel in panels] == [
            "OpenAI",
            "Claude",
            "Grok",
        ]
        assert [panel["source"] for panel in panels] == [
            "openai",
            "claude",
            "grok",
        ]
        assert [panel["status"] for panel in panels] == [
            "FINDING",
            "ABSTAIN",
            "ERROR",
        ]
        assert [panel["level"] for panel in panels] == ["INFO", "WARN", "ERROR"]
        assert json.loads(json.dumps(panels)) == panels

    def test_a_finding_pane_carries_text_facts_and_evidence(self) -> None:
        runner, controller, core = review_controller()
        controller.submit("run_review")
        controller.apply_result(runner.run_next(core))

        (openai, _claude, _grok) = controller.provider_panels()
        facts = dict(openai["facts"])

        assert openai["response"] == "the module boundary is clear"
        assert openai["body_lines"][0] == "the module boundary is clear"
        assert "  - application/context.py:12" in openai["body_lines"]
        assert openai["evidence"] == ["application/context.py:12"]
        assert openai["status_text"] == "finding returned"
        assert facts["Severity"] == "MEDIUM"
        assert facts["Confidence"] == "0.6"
        assert facts["Relation"] == "UNRESOLVED"
        assert facts["Anchor"] == "-"
        assert facts["Step"] == "9"
        assert facts["Finding id"] == "finding-openai-9-abc123abc123"
        assert facts["Evidence refs"] == "1"
        assert facts["Tokens"] == "10 in / 5 out"
        assert "0.0100 USD" in facts["Cost"]

    def test_an_abstain_and_an_error_pane_carry_their_reason(self) -> None:
        runner, controller, core = review_controller()
        controller.submit("run_review")
        controller.apply_result(runner.run_next(core))

        (_openai, claude, grok) = controller.provider_panels()

        assert claude["status_text"] == "abstained (no finding)"
        assert claude["response"] == ""
        assert "abstained and produced no finding" in claude["body_lines"][0]
        assert any(
            "ClaudeAdvisorAbstainError" in line for line in claude["body_lines"]
        )
        assert dict(claude["facts"])["Tokens"] == "-"

        assert grok["status_text"] == "provider error"
        assert "failed and produced no finding" in grok["body_lines"][0]
        assert any(
            "GrokMissingApiKeyError" in line for line in grok["body_lines"]
        )
        assert dict(grok["facts"])["Evidence refs"] == "0"

    def test_the_panes_are_labelled_before_the_first_review(self) -> None:
        _runner, controller, _core = review_controller()

        panels = controller.provider_panels()

        assert [panel["name"] for panel in panels] == [
            "OpenAI",
            "Claude",
            "Grok",
        ]
        assert {panel["status_text"] for panel in panels} == {"not run yet"}
        assert {panel["configured"] for panel in panels} == {True}
        assert "has not answered" in panels[0]["body_lines"][0]

    def test_a_missing_advisor_is_padded_never_invented(self) -> None:
        _runner, controller, _core = review_controller(reviewers=["OpenAI"])

        panels = controller.provider_panels()

        assert len(panels) == 3
        assert [panel["name"] for panel in panels] == [
            "OpenAI",
            "Advisor 2",
            "Advisor 3",
        ]
        assert panels[1]["configured"] is False
        assert "No advisor is configured" in panels[1]["body_lines"][0]

    def test_a_fourth_advisor_would_get_its_own_pane(self) -> None:
        reviewed = make_review()
        reviewed["providers"].append(
            {
                "source": "fourth",
                "status": "FINDING",
                "reason": "",
                "finding": {
                    "id": "finding-fourth-9-abc",
                    "source": "fourth",
                    "claim": "a fourth opinion",
                    "evidence": [],
                    "confidence": 0.4,
                    "severity": "LOW",
                    "step_no": 9,
                },
                "relation": "UNRESOLVED",
                "relation_target": None,
                "cost": {"available": False, "reason": "no record"},
            }
        )
        runner, controller, core = review_controller(review=reviewed)
        controller.submit("run_review")
        controller.apply_result(runner.run_next(core))

        panels = controller.provider_panels()

        assert len(panels) == 4
        assert panels[3]["name"] == "fourth"
        assert panels[3]["response"] == "a fourth opinion"

    def test_the_shared_sections_sit_below_the_panes(self) -> None:
        runner, controller, core = review_controller()
        controller.submit("run_review")
        controller.apply_result(runner.run_next(core))

        merged = dict(controller.merged_evidence_rows())
        assert merged["Findings"] == "1"
        assert merged["Abstained"] == "1"
        assert merged["Errors"] == "1"
        assert merged["Gate status"] == "ACCEPTED"
        assert merged["Gate anchors"] == "unknown-layer"
        assert "claude" in merged["Abstained by"]
        assert "grok" in merged["Failed"]

        assert controller.review_conflict_rows() == []
        judge = "\n".join(controller.judge_lines())
        assert "Status:     NOT_CONSULTED" in judge
        assert "Reason: no evidence conflict was merged" in judge
        decision = "\n".join(controller.decision_lines())
        assert "Status:      ACCEPTED" in decision
        assert "only verdict" in decision
        assert [row[0] for row in controller.cost_rows()] == [
            "openai",
            "claude",
            "grok",
            "judge",
            "total",
        ]
        assert controller.total_cost_text().endswith("0 in / 0 out tokens")

    def test_a_failed_judge_still_renders_the_whole_review(self) -> None:
        reviewed = make_review(
            judge={
                "available": True,
                "consulted": True,
                "status": "ERROR",
                "reason": "JudgeProviderError",
                "count": 0,
                "judgments": [],
            },
            conflicts=[
                {
                    "target": "unknown-layer",
                    "supporting_ids": ["finding-openai-9-abc123abc123"],
                    "contradicting_ids": ["finding-claude-9-abc123abc123"],
                }
            ],
        )
        runner, controller, core = review_controller(review=reviewed)
        controller.submit("run_review")
        controller.apply_result(runner.run_next(core))

        # the panes and every shared section still render, with the failure stated
        assert [panel["status"] for panel in controller.provider_panels()] == [
            "FINDING",
            "ABSTAIN",
            "ERROR",
        ]
        judge = "\n".join(controller.judge_lines())
        assert "Status:     ERROR" in judge
        assert "JudgeProviderError" in judge
        merged = dict(controller.merged_evidence_rows())
        assert merged["Errors"] == "1"
        assert merged["Gate status"] == "ACCEPTED"
        assert "Status:      ACCEPTED" in "\n".join(
            controller.decision_lines()
        )
        assert [row[0] for row in controller.cost_rows()][-1] == "total"
        assert controller.review_conflict_rows() == [
            [
                "unknown-layer",
                "finding-openai-9-abc123abc123",
                "finding-claude-9-abc123abc123",
            ]
        ]
        assert controller.step_count() == 0

    def test_the_empty_review_explains_itself(self) -> None:
        _runner, controller, _core = review_controller()

        panels = controller.provider_panels()

        assert [panel["status_text"] for panel in panels] == ["not run yet"] * 3
        assert controller.merged_evidence_rows() == [
            ["Merged evidence", "no architecture review yet"]
        ]
        assert controller.review_conflict_rows() == [
            ["-", "no architecture review yet", "-"]
        ]
        assert controller.judge_lines() == [
            "No architecture review has been run in this session."
        ]
        assert controller.decision_lines() == [
            "No architecture review has been run in this session."
        ]
        assert controller.cost_rows() == [
            ["total", "no architecture review yet", "-"]
        ]
        assert controller.total_cost_text() == "no architecture review yet"

    def test_progress_lines_are_shown_and_cleared_on_finish(self) -> None:
        runner, controller, core = review_controller()

        controller.submit("run_review")
        controller.drain_progress(
            ["deterministic gate check", "advisor: openai"]
        )
        assert controller.review_progress == "advisor: openai"
        assert "advisor: openai" in controller.status
        assert (
            controller.view_model()["review"]["progress"] == "advisor: openai"
        )

        controller.apply_result(runner.run_next(core))

        assert controller.review_progress == ""
        assert controller.view_model()["review"]["progress"] == ""

    def test_a_failed_review_keeps_the_last_payload_and_latches_the_error(
        self,
    ) -> None:
        runner, controller, core = review_controller()
        controller.submit("run_review")
        controller.apply_result(runner.run_next(core))

        controller.submit("run_review")
        controller.apply_result(
            JobResult(label="run_review", error=describe_error(_critical_error()))
        )

        assert controller.is_critical is True
        assert controller.review is not None
        # a read-only diagnostic stays available under CRITICAL; mutations do not
        assert controller.enabled(controller.intent("run_review")) is True
        assert controller.enabled(controller.intent("run_until_idle")) is False
        assert controller.enabled(controller.intent("approve")) is False

    def test_the_review_cannot_verify_or_mutate_anything(self) -> None:
        runner, controller, core = review_controller()

        controller.submit("run_review")
        controller.apply_result(runner.run_next(core))

        # the review job asked the core for exactly one thing, plus one read
        assert core.call_names == ["run_architecture_review", "payload"]
        for forbidden in (
            "run_until_idle",
            "approve",
            "reject",
            "unblock",
            "resolve",
            "abort",
            "pause_project",
            "resume_project",
            "import_plan",
        ):
            assert forbidden not in core.call_names
        assert "verify" not in {intent.key for intent in INTENTS}


def proposal_controller(
    *,
    actor: str = "operator",
    board: Any = None,
) -> tuple[FakeRunner, GuiController, RecordingWorker]:
    """A controller holding one proposal board, with a recording runner."""
    runner = FakeRunner()
    controller = GuiController(runner, actor=actor, reports_dir="reports")
    controller.apply_result(
        JobResult(
            label="refresh",
            payload={
                "payload": make_payload(),
                "audit": [],
                "board": make_board() if board is None else board,
            },
        )
    )
    return runner, controller, RecordingWorker()


class TestProposalPanel:
    """The Architecture Proposal tab: plain data in, one core call out."""

    def test_the_view_model_carries_a_plain_proposal_section(self) -> None:
        _runner, controller, _core = proposal_controller()

        proposal = controller.view_model()["proposal"]

        assert set(proposal) == {
            "available",
            "review_available",
            "synthesizer",
            "count",
            "requirement",
            "feedback",
            "status",
            "header",
            "sections",
            "digest_rows",
            "history_rows",
        }
        assert isinstance(proposal["status"], str)
        assert proposal["available"] is True
        assert all(
            isinstance(field, str) and isinstance(value, str)
            for field, value in proposal["header"]
        )

    def test_the_view_model_is_json_serialisable(self) -> None:
        """No core object, no datetime, no repository ever reaches the view."""
        _runner, controller, _core = proposal_controller()

        encoded = json.dumps(controller.view_model())

        assert isinstance(encoded, str)
        assert "proposal-1" in encoded

    def test_every_proposed_section_is_a_table_of_rows(self) -> None:
        _runner, controller, _core = proposal_controller()

        sections = controller.view_model()["proposal"]["sections"]

        assert set(sections) == {
            "facts",
            "modules",
            "data_flows",
            "external_dependencies",
            "risks",
            "adr_candidates",
            "implementation_phases",
        }
        for name, rows in sections.items():
            assert isinstance(rows, list), name
            for row in rows:
                assert isinstance(row, list), name
                assert all(isinstance(cell, str) for cell in row), name

    def test_the_evidence_digest_is_shown_as_traceability(self) -> None:
        _runner, controller, _core = proposal_controller()

        rows = dict(controller.view_model()["proposal"]["digest_rows"])

        assert rows["Review id"] == "architecture-review-9-abcdef012345"
        assert "3" in rows["Advisors"]
        assert "Deterministic gate (assistant)" in rows

    def test_the_history_lists_every_stored_proposal(self) -> None:
        _runner, controller, _core = proposal_controller()

        rows = controller.view_model()["proposal"]["history_rows"]

        assert len(rows) == 1
        assert rows[0][0] == "proposal-1"
        assert rows[0][2] == "DRAFT"

    def test_generate_is_refused_without_a_review(self) -> None:
        _runner, controller, _core = proposal_controller(
            board=make_board(review_available=False, latest=None, count=0)
        )

        intent = controller.intent("synthesize_proposal")

        assert controller.enabled(intent) is False
        assert "review" in controller.refusal(intent)
        assert controller.view_model()["proposal"]["available"] is False

    def test_generate_is_available_with_a_review(self) -> None:
        _runner, controller, _core = proposal_controller()

        intent = controller.intent("synthesize_proposal")

        assert controller.enabled(intent) is True
        assert controller.refusal(intent, "design the pilot") is None

    def test_the_decisions_need_a_draft_proposal(self) -> None:
        _runner, controller, _core = proposal_controller(
            board=make_board(
                latest=make_proposal(status="APPROVED", decided_by="operator")
            )
        )

        for key in (
            "approve_proposal",
            "reject_proposal",
            "request_proposal_revision",
        ):
            intent = controller.intent(key)
            assert controller.enabled(intent) is False, key
            assert "DRAFT" in controller.refusal(intent), key

    def test_the_decisions_are_available_for_a_draft(self) -> None:
        _runner, controller, _core = proposal_controller()

        for key in ("approve_proposal", "reject_proposal"):
            assert controller.enabled(controller.intent(key)) is True, key

    def test_requesting_a_revision_needs_feedback(self) -> None:
        _runner, controller, _core = proposal_controller()
        intent = controller.intent("request_proposal_revision")

        assert "eedback" in controller.refusal(intent)

        controller.set_revision_feedback("split the transcriber")
        assert controller.refusal(intent, "not yet") is None

    def test_generate_calls_the_core_once_with_the_typed_inputs(self) -> None:
        runner, controller, core = proposal_controller()
        controller.set_proposal_requirement("MP3 -> TXT")

        assert (
            controller.submit("synthesize_proposal", reason="design it") is True
        )
        result = runner.run_next(core)

        assert result.ok is True
        assert core.call_names.count("synthesize_proposal") == 1
        _name, kwargs = core.calls[0]
        assert kwargs["requirement"] == "MP3 -> TXT"
        assert kwargs["actor"] == "operator"
        assert kwargs["reason"] == "design it"
        assert kwargs["revision_of"] is None

    def test_approve_calls_the_core_once_for_the_current_proposal(self) -> None:
        runner, controller, core = proposal_controller()

        assert controller.submit("approve_proposal", reason="accepted") is True
        result = runner.run_next(core)

        assert result.ok is True
        assert core.call_names.count("approve_proposal") == 1
        _name, kwargs = core.calls[0]
        assert kwargs["proposal_id"] == "proposal-1"
        assert kwargs["actor"] == "operator"
        assert kwargs["reason"] == "accepted"

    def test_reject_calls_the_core_once_for_the_current_proposal(self) -> None:
        runner, controller, core = proposal_controller()

        controller.submit("reject_proposal", reason="not this design")
        result = runner.run_next(core)

        assert result.ok is True
        _name, kwargs = core.calls[0]
        assert kwargs["proposal_id"] == "proposal-1"
        assert kwargs["reason"] == "not this design"

    def test_a_revision_request_carries_the_feedback(self) -> None:
        runner, controller, core = proposal_controller()
        controller.set_revision_feedback("split the transcriber")

        controller.submit("request_proposal_revision", reason="not yet")
        result = runner.run_next(core)

        assert result.ok is True
        _name, kwargs = core.calls[0]
        assert kwargs["proposal_id"] == "proposal-1"
        assert kwargs["feedback"] == "split the transcriber"

    def test_a_generate_after_a_revision_request_replaces_that_proposal(
        self,
    ) -> None:
        runner, controller, core = proposal_controller(
            board=make_board(
                latest=make_proposal(
                    status="REVISION_REQUESTED",
                    revision_feedback="split the transcriber",
                )
            )
        )

        controller.submit("synthesize_proposal", reason="regenerate")
        result = runner.run_next(core)

        assert result.ok is True
        _name, kwargs = core.calls[0]
        assert kwargs["revision_of"] == "proposal-1"

    def test_a_decision_result_is_reported_and_re_rendered(self) -> None:
        runner, controller, core = proposal_controller()

        controller.submit("approve_proposal", reason="accepted")
        core._board = make_board(
            latest=make_proposal(status="APPROVED", decided_by="operator")
        )
        controller.apply_result(runner.run_next(core))

        assert "APPROVED" in controller.status
        view = controller.view_model()["proposal"]
        assert view["sections"]["facts"][0][0] == "Summary"

    def test_the_proposal_actions_never_touch_another_core_path(self) -> None:
        runner, controller, core = proposal_controller()
        controller.set_proposal_requirement("MP3 -> TXT")

        controller.submit("synthesize_proposal", reason="design it")
        runner.run_next(core)

        for forbidden in (
            "run_until_idle",
            "approve",
            "reject",
            "unblock",
            "resolve",
            "abort",
            "pause_project",
            "resume_project",
            "import_plan",
            "run_architecture_review",
        ):
            assert forbidden not in core.call_names

    def test_the_proposal_tab_cannot_verify_or_delete_anything(self) -> None:
        keys = {intent.key for intent in INTENTS}

        assert not [key for key in keys if "verify" in key]
        assert not [key for key in keys if "proposal" in key and "delete" in key]


SUPERVISOR_KEYS = (
    "analyze_report",
    "approve_and_send",
    "reject_directive",
    "waive_supervision",
    "escalate_supervision",
)


def supervisor_controller(
    *,
    actor: str = "operator",
    status: Optional[dict[str, Any]] = None,
) -> tuple[FakeRunner, GuiController, RecordingWorker]:
    """A controller holding one supervision board, with a recording runner."""
    board = make_supervisor_status() if status is None else status
    runner = FakeRunner()
    controller = GuiController(runner, actor=actor, reports_dir="reports")
    controller.apply_result(
        JobResult(
            label="refresh",
            payload={
                "payload": make_payload(state="REPORT_RECEIVED"),
                "audit": [],
                "supervisor": board,
            },
        )
    )
    return runner, controller, RecordingWorker(supervisor=board)


def _rows(pane: dict[str, Any]) -> dict[str, str]:
    """One pane's label/value rows as a dict, for readable assertions."""
    return {label: value for label, value in pane["rows"]}


class TestSupervisorPanel:
    """The Supervisor tab: plain data in, one core call out, nothing else."""

    def test_the_view_model_carries_a_plain_supervisor_section(self) -> None:
        _runner, controller, _core = supervisor_controller()

        view = controller.view_model()["supervisor"]

        assert set(view) == {
            "enabled",
            "available",
            "status",
            "waiting_for",
            "instruction",
            "header",
            "assistant_pane",
            "cline_pane",
            "supervisor_pane",
            "history_rows",
        }
        assert view["enabled"] is True
        assert view["available"] is True
        assert isinstance(view["status"], str)
        assert all(
            isinstance(row, list) and len(row) == 2 for row in view["header"]
        )
        assert all(isinstance(cell, str) for row in view["header"] for cell in row)
        for key in ("assistant_pane", "cline_pane", "supervisor_pane"):
            pane = view[key]
            assert isinstance(pane["title"], str)
            for row in pane["rows"]:
                assert len(row) == 2
                assert all(isinstance(cell, str) for cell in row)

    def test_the_view_model_is_json_serialisable(self) -> None:
        """No core object, no datetime, no repository ever reaches the view."""
        _runner, controller, _core = supervisor_controller()

        encoded = json.dumps(controller.view_model())

        assert isinstance(encoded, str)
        assert "WAITING_HUMAN" in encoded
        assert "a" * 64 not in encoded  # identities are shortened for display

    def test_the_three_panes_are_the_assistant_the_worker_and_the_supervisor(
        self,
    ) -> None:
        _runner, controller, _core = supervisor_controller()

        view = controller.view_model()["supervisor"]

        assert view["assistant_pane"]["title"] == "Architecture Assistant"
        assert view["cline_pane"]["title"] == "Cline"
        assert view["supervisor_pane"]["title"] == "Supervisor"

        assistant = _rows(view["assistant_pane"])
        assert assistant["Task goal"] == "GUI step"
        assert assistant["Task"] == "do the thing"
        assert assistant["Attempts remaining"] == "2"
        assert assistant["Constraints"] == "keep it Python-only"
        assert assistant["Accepted ADRs"] == "ADR-001: sqlite only"
        assert assistant["Open risks"] == "risk-001: pilot risk"
        assert assistant["Deterministic findings"] == "finding-1: no violation"
        assert assistant["Open change requests"] == "ACR-001: add monitor"

        cline = _rows(view["cline_pane"])
        assert cline["Report status"] == "DONE"
        assert cline["Report summary"] == "the step is done"
        assert cline["Files created"] == "src/new.py"
        assert cline["Tests"] == "failed=0 | passed=3"
        assert cline["Issues"] == "a note"
        assert cline["Report hash"] == "b" * 64

        supervisor = _rows(view["supervisor_pane"])
        assert supervisor["Provider"] == "scripted"
        assert supervisor["Action"] == "CLARIFY"
        assert supervisor["Risk"] == "LOW"
        assert supervisor["Reason"] == "the report is missing its test command"
        assert supervisor["Evidence"] == "application/context.py:12"
        assert (
            supervisor["Proposed instruction"]
            == "Add the pytest command to the report."
        )
        assert supervisor["Requires human"] == "yes"

    def test_the_header_shows_the_identity_the_state_and_the_polling_status(
        self,
    ) -> None:
        _runner, controller, _core = supervisor_controller()

        header = dict(controller.view_model()["supervisor"]["header"])

        assert header["Project"] == "Project"
        assert header["Step"] == "9"
        assert header["Attempt"] == "1"
        assert header["Worker state"] == "REPORT_RECEIVED"
        assert header["Supervisor state"] == "WAITING_HUMAN"
        assert header["Current report hash"] == "b" * 16
        assert header["Supervision id"] == "a" * 16
        assert header["Polling status"] == "running"
        assert header["Waiting For"] == "human-decision"

    def test_the_polling_status_is_honest_when_nothing_reports(self) -> None:
        for runtime, expected in (
            ({"state": "RUNNING", "running": True}, "running"),
            ({"state": "STOPPED", "running": False}, "stopped"),
            ({}, "not reporting"),
        ):
            _runner, controller, _core = supervisor_controller(
                status=make_supervisor_status(runtime=runtime)
            )

            header = dict(controller.view_model()["supervisor"]["header"])

            assert header["Polling status"] == expected, runtime

    def test_the_gate_verdict_and_the_malformed_deadlines_are_shown(self) -> None:
        _runner, controller, _core = supervisor_controller()

        row = _rows(controller.view_model()["supervisor"]["supervisor_pane"])

        assert row["Gate"] == "BLOCK | supervision-waiting_human"
        assert row["Malformed deadlines"] == "stability 30.0s | absolute 120.0s"

        _runner, allowed, _core = supervisor_controller(
            status=make_supervisor_status(
                allowed=True, verdict_reason="supervision-no_action"
            )
        )

        allowed_row = _rows(allowed.view_model()["supervisor"]["supervisor_pane"])
        assert allowed_row["Gate"] == "ALLOW | supervision-no_action"

    def test_the_history_lists_every_identity_of_the_attempt_newest_first(
        self,
    ) -> None:
        older = make_supervision_record(
            supervision_id="1" * 64,
            status="MALFORMED",
            action=None,
            source_report_hash="9" * 64,
        )
        _runner, controller, _core = supervisor_controller(
            status=make_supervisor_status(
                history=[make_supervision_record(), older]
            )
        )

        rows = controller.view_model()["supervisor"]["history_rows"]

        assert len(rows) == 2
        assert rows[0][1] == "WAITING_HUMAN"
        assert rows[1][1] == "MALFORMED"
        assert rows[1][2] == "-"  # no action for a malformed record
        assert rows[1][4] == "9" * 16

    def test_disabled_supervision_explains_itself_and_offers_nothing(self) -> None:
        _runner, controller, _core = supervisor_controller(
            status=make_supervisor_status(
                enabled=False,
                reason="there is no current step to supervise",
                runtime={"state": "STOPPED", "running": False},
            )
        )

        view = controller.view_model()["supervisor"]

        assert view["enabled"] is False
        assert "disabled" in view["status"]
        for key in SUPERVISOR_KEYS:
            intent = controller.intent(key)
            assert controller.enabled(intent) is False, key
            assert controller.supervisor_action_available(key) is False, key
            assert "disabled" in str(controller.supervisor_refusal(key)), key
            assert controller.refusal(intent, "a reason") is not None, key

    def test_no_report_means_there_is_nothing_to_analyse(self) -> None:
        _runner, controller, _core = supervisor_controller(
            status=make_supervisor_status(
                record=None,
                report_hash=None,
                context=None,
                verdict_reason="supervision-no-report",
            )
        )

        assert controller.supervisor_action_available("analyze_report") is False
        assert "worker report" in str(
            controller.supervisor_refusal("analyze_report")
        )

        for key in SUPERVISOR_KEYS[1:]:
            assert "analyze it first" in str(
                controller.supervisor_refusal(key)
            ), key

    def test_a_waiting_directive_needs_an_instruction(self) -> None:
        record = make_supervision_record(instruction_for_cline="")
        _runner, controller, _core = supervisor_controller(
            status=make_supervisor_status(record=record)
        )

        assert controller.supervision_directive() == ""
        assert controller.supervisor_action_available("approve_and_send") is False
        assert controller.enabled(controller.intent("approve_and_send")) is False

        controller.set_instruction("Add the pytest command.")
        assert controller.instruction == "Add the pytest command."
        assert controller.supervisor_action_available("approve_and_send") is True

        controller.set_instruction("   ")
        assert controller.supervisor_action_available("approve_and_send") is False

    def test_the_records_own_instruction_is_enough_to_send(self) -> None:
        _runner, controller, _core = supervisor_controller()

        assert controller.supervision_directive() == (
            "Add the pytest command to the report."
        )
        assert controller.supervisor_action_available("approve_and_send") is True
        intent = controller.intent("approve_and_send")
        assert controller.refusal(intent, "it is bounded") is None

    def test_a_sent_directive_cannot_be_decided_again(self) -> None:
        _runner, controller, _core = supervisor_controller(
            status=make_supervisor_status(
                record=make_supervision_record(
                    status="SENT", sent_at="2026-09-21T22:00:00+00:00"
                )
            )
        )

        assert controller.supervision_status() == "SENT"
        for key in SUPERVISOR_KEYS[1:]:
            assert controller.supervisor_action_available(key) is False, key
            assert "already sent" in str(controller.supervisor_refusal(key)), key

    def test_the_decisions_follow_the_status(self) -> None:
        """Availability mirrors the domain policy: fail-closed, no second guess."""
        decisions = (
            "approve_and_send",
            "reject_directive",
            "waive_supervision",
            "escalate_supervision",
        )
        cases = (
            ("WAITING_HUMAN", frozenset(decisions)),
            ("READY_TO_SEND", frozenset(decisions)),
            ("ESCALATED", frozenset({"reject_directive", "waive_supervision"})),
            ("ERROR", frozenset({"reject_directive", "waive_supervision", "escalate_supervision"})),
            ("MALFORMED", frozenset({"reject_directive", "waive_supervision", "escalate_supervision"})),
            ("STALE", frozenset({"reject_directive", "waive_supervision", "escalate_supervision"})),
            ("REJECTED", frozenset()),
            ("WAIVED", frozenset()),
        )
        for status, expected in cases:
            _runner, controller, _core = supervisor_controller(
                status=make_supervisor_status(
                    record=make_supervision_record(status=status)
                )
            )

            assert controller.supervision_status() == status
            for key in decisions:
                available = controller.supervisor_action_available(key)
                assert available is (key in expected), (status, key)
                if not available:
                    refusal = str(controller.supervisor_refusal(key))
                    assert status in refusal, (status, key, refusal)

    def test_analyze_calls_the_core_once(self) -> None:
        runner, controller, core = supervisor_controller()

        assert controller.submit("analyze_report", reason="supervise it") is True
        result = runner.run_next(core)

        assert result.ok is True
        assert core.call_names.count("analyze_report") == 1
        assert core.call_names[0] == "analyze_report"
        assert controller.status == "Analyze Report requested."

    def test_approve_and_send_calls_the_core_once_with_the_verbatim_instruction(
        self,
    ) -> None:
        runner, controller, core = supervisor_controller()
        controller.set_instruction("  Add the pytest command.  ")

        assert controller.submit("approve_and_send", reason="bounded repair") is True
        result = runner.run_next(core)

        assert result.ok is True
        assert core.call_names.count("approve_and_send") == 1
        _name, kwargs = core.calls[0]
        assert kwargs["instruction"] == "  Add the pytest command.  "
        assert kwargs["actor"] == "operator"
        assert kwargs["reason"] == "bounded repair"

    def test_the_instruction_is_captured_on_the_ui_thread(self) -> None:
        """The core thread must never read controller state."""
        runner, controller, core = supervisor_controller()
        controller.set_instruction("the submitted text")

        controller.submit("approve_and_send", reason="bounded repair")
        controller.set_instruction("typed later")

        runner.run_next(core)

        _name, kwargs = core.calls[0]
        assert kwargs["instruction"] == "the submitted text"
        assert controller.instruction == "typed later"

    def test_every_human_decision_calls_the_core_once(self) -> None:
        expected = {
            "approve_and_send": "Approve & Send requested.",
            "reject_directive": "Reject Directive requested.",
            "waive_supervision": "Waive requested.",
            "escalate_supervision": "Escalate requested.",
        }
        for key, message in expected.items():
            runner, controller, core = supervisor_controller()

            assert controller.submit(key, reason="operator decided") is True
            result = runner.run_next(core)

            assert result.ok is True, key
            assert core.call_names.count(key) == 1, key
            _name, kwargs = core.calls[0]
            assert kwargs["actor"] == "operator", key
            assert kwargs["reason"] == "operator decided", key
            assert "supervision_id" not in kwargs, key  # the core resolves it
            assert controller.status == message, key

    def test_the_human_decisions_need_an_actor_and_a_reason(self) -> None:
        runner, controller, core = supervisor_controller(actor="")

        assert controller.submit("waive_supervision", reason="because") is False
        assert runner.jobs == []
        assert "actor" in controller.status.lower()

        runner, named, core = supervisor_controller()
        assert named.submit("waive_supervision", reason="   ") is False
        assert "reason" in named.status.lower()
        assert runner.jobs == []
        assert core.call_names == []

    def test_a_decision_result_is_reported_and_re_rendered(self) -> None:
        runner, controller, core = supervisor_controller()

        controller.submit("waive_supervision", reason="checked by hand")
        core._supervisor = make_supervisor_status(
            record=make_supervision_record(status="WAIVED", decided_by="operator"),
            allowed=True,
            waiting_for="authoritative-review",
            verdict_reason="supervision-waived",
        )
        controller.apply_result(runner.run_next(core))

        view = controller.view_model()["supervisor"]
        assert view["status"].startswith("Supervision WAIVED")
        assert "authoritative-review" in view["status"]
        header = dict(view["header"])
        assert header["Supervisor state"] == "WAIVED"
        assert controller.supervision_waiting_for() == "authoritative-review"

    def test_the_supervisor_actions_never_touch_another_core_path(self) -> None:
        runner, controller, core = supervisor_controller()
        controller.set_instruction("Add the pytest command.")

        controller.submit("analyze_report", reason="supervise it")
        runner.run_next(core)

        for forbidden in (
            "run_until_idle",
            "approve",
            "reject",
            "unblock",
            "resolve",
            "abort",
            "pause_project",
            "resume_project",
            "import_plan",
            "run_architecture_review",
            "synthesize_proposal",
        ):
            assert forbidden not in core.call_names, forbidden

    def test_the_supervisor_tab_cannot_verify_or_move_the_workflow(self) -> None:
        keys = {intent.key for intent in INTENTS}

        assert not [key for key in keys if "verify" in key]
        assert not [
            key
            for key in keys
            if key in SUPERVISOR_KEYS and key.endswith("_step")
        ]
        assert not [key for key in keys if "attempt" in key]
