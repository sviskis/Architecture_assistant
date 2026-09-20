"""Tests for Step 11 - Architecture Evolution (persistent change requests).

Covers the mandated properties: a request is authoritative state that survives a
restart, the lifecycle is strictly ``PROPOSED -> APPROVED -> APPLIED`` (or
``PROPOSED -> REJECTED``, terminal both ways), only a **persisted** approval can
move a baseline, one operation that touches several aggregates happens in one
transaction boundary, a repeated ``apply`` is a pure no-op, the ADR link is
structural (never parsed from prose), the code baseline stays authoritative and
the version history is never rewritten.
"""

from __future__ import annotations

import inspect
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import architecture_assistant.application.architecture_evolution as evolution_module
from architecture_assistant.application import (
    AdrSpec,
    ArchitectureEvolution,
    ArchitectureEvolutionConflictError,
    ArchitectureEvolutionError,
    ArchitectureEvolutionNotApprovedError,
    ArchitectureEvolutionNotFoundError,
    ArchitectureVersioning,
    BootstrapOutcome,
    RiskSpec,
)
from architecture_assistant.architecture import (
    APPLICATION_LAYER,
    ARCHITECTURE_CURRENT,
    ARCHITECTURE_LAYER,
    ARCHITECTURE_V1,
    ARCHITECTURE_V1_1,
    COMPOSITION_LAYER,
    INFRASTRUCTURE_LAYER,
    ROOT_PACKAGE,
    ArchitectureValidator,
    layer_of,
    scan_directory,
)
from architecture_assistant.composition import (
    COMPOSITION_LAYER_DRIFT_RISK,
    COMPOSITION_ROOT_ROADMAP_IMPACT,
    DECLARED_CHANGES,
    DEFAULT_APPROVER,
    V1_1_ACR_ID,
    build_evolution,
    canonical_versions,
    change_v1_0_to_v1_1,
    declared_risks,
    reconcile_declared_changes,
)
from architecture_assistant.domain.audit import AuditEntityType
from architecture_assistant.domain.enums import (
    ACRStatus,
    ADRStatus,
    RiskStatus,
    Severity,
)
from architecture_assistant.domain.models import (
    ArchitectureChangeRequest,
    ArchitectureVersion,
    Risk,
)
from architecture_assistant.infrastructure import (
    SqliteStorage,
    close_database,
    open_database,
)

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)

#: The version the synthetic change moves to. The code side declares only v1.0
#: and v1.1, so the tests inject this record explicitly - the engine itself may
#: never invent a baseline.
V1_2: ArchitectureVersion = ArchitectureVersion(
    version="1.2",
    baseline="Architecture v1.2 - synthetic baseline used only by these tests.",
    rules=("unknown-layer",),
    is_current=True,
    created_at=NOW,
)

SPLIT_ADR = AdrSpec(
    id="ADR-009",
    title="Split the scanner from the validator",
    rationale="scanning and evaluating are two responsibilities.",
    decision="The scanner only builds the source model; the validator owns rules.",
    consequences=("The validator can be tested without touching the file system.",),
    related=("ADR-003",),
)


def fixed_clock() -> datetime:
    return NOW


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


@contextmanager
def opened(path):
    """Open a source of truth, hand it out, and always close it again.

    Re-opening the same path is how these tests simulate a restart: nothing but
    the database file survives.
    """
    connection = open_database(path)
    try:
        yield SqliteStorage(connection)
    finally:
        close_database(connection)


def make_engine(
    storage: SqliteStorage,
    *,
    extra_versions: tuple[ArchitectureVersion, ...] = (),
    risks=None,
) -> ArchitectureEvolution:
    """An engine wired exactly like the composition root wires it."""
    versions = dict(canonical_versions(NOW))
    for record in extra_versions:
        versions[record.version] = record
    return ArchitectureEvolution(
        storage,
        canonical_versions=versions,
        clock=fixed_clock,
        declared_risks=risks,
        versioning=ArchitectureVersioning(storage, clock=fixed_clock),
    )


def seed_history(storage: SqliteStorage, *, current: str = "1.0") -> None:
    """Store the declared baselines with correct currentness (no rewriting)."""
    canonical = canonical_versions(NOW)
    if current == ARCHITECTURE_V1_1.version:
        storage.architecture_versions.upsert(
            replace(
                canonical[ARCHITECTURE_V1.version],
                is_current=False,
                superseded_by=ARCHITECTURE_V1_1.version,
            )
        )
        storage.architecture_versions.upsert(
            canonical[ARCHITECTURE_V1_1.version]
        )
        return
    storage.architecture_versions.upsert(canonical[ARCHITECTURE_V1.version])


def make_request(**overrides) -> ArchitectureChangeRequest:
    """A second, non-historical change request (v1.1 -> v1.2)."""
    payload = {
        "request_id": "ACR-002",
        "title": SPLIT_ADR.title,
        "rationale": SPLIT_ADR.rationale,
        "source_version": ARCHITECTURE_V1_1.version,
        "target_version": V1_2.version,
        "rule_ids": V1_2.rules,
        "adr_id": SPLIT_ADR.id,
        "roadmap_impact": ("step-012",),
        "created_at": NOW,
    }
    payload.update(overrides)
    return ArchitectureChangeRequest(**payload)


@pytest.fixture
def database_path(tmp_path) -> Path:
    return tmp_path / "evolution.db"


@pytest.fixture
def storage() -> SqliteStorage:
    connection = open_database(":memory:")
    yield SqliteStorage(connection)
    close_database(connection)


@pytest.fixture
def engine(storage) -> ArchitectureEvolution:
    return make_engine(storage, extra_versions=(V1_2,))


class TestLifecycle:
    """``PROPOSED -> APPROVED -> APPLIED`` and ``PROPOSED -> REJECTED`` only."""

    def test_propose_registers_the_request_and_its_adr_atomically(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)

        summary = engine.propose(make_request(), adr=SPLIT_ADR)

        assert summary.status is ACRStatus.PROPOSED
        assert summary.outcome is BootstrapOutcome.CREATED
        assert summary.roadmap_impact == ("step-012",)
        assert storage.change_requests.get("ACR-002") == make_request()
        adr = storage.adrs.get(SPLIT_ADR.id)
        assert adr is not None
        assert adr.status is ADRStatus.PROPOSED
        assert adr.version == 1
        assert adr.context.startswith(SPLIT_ADR.provenance)
        entries = storage.audit.list()
        assert tuple(
            (entry.entity_type, entry.entity_id, entry.action.value)
            for entry in entries
        ) == (
            (AuditEntityType.ACR, "ACR-002", "CREATE"),
            (AuditEntityType.ADR, SPLIT_ADR.id, "CREATE"),
        )
        assert entries[0].detail["status_to"] == "PROPOSED"
        assert entries[0].detail["target_version"] == V1_2.version

    def test_propose_is_idempotent(self, storage, engine) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        audit = storage.audit.list()
        adrs = storage.adrs.list()

        again = engine.propose(make_request(), adr=SPLIT_ADR)

        assert again.outcome is BootstrapOutcome.ALREADY_PRESENT
        assert again.status is ACRStatus.PROPOSED
        assert storage.audit.list() == audit
        assert storage.adrs.list() == adrs

    def test_propose_refuses_to_reuse_a_request_id_with_other_content(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)

        with pytest.raises(
            ArchitectureEvolutionConflictError, match="different content"
        ):
            engine.propose(make_request(title="Something else"), adr=SPLIT_ADR)

    def test_propose_requires_the_request_to_agree_with_its_adr(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)

        with pytest.raises(
            ArchitectureEvolutionConflictError, match="links ADR"
        ):
            engine.propose(make_request(adr_id="ADR-010"), adr=SPLIT_ADR)

    def test_propose_only_takes_a_fresh_proposed_request(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)

        with pytest.raises(ValueError, match="fresh PROPOSED"):
            engine.propose(
                make_request(
                    status=ACRStatus.APPROVED,
                    approved_by="architect",
                    approved_at=NOW,
                ),
                adr=SPLIT_ADR,
            )

    def test_an_unknown_request_is_not_found(self, engine) -> None:
        with pytest.raises(ArchitectureEvolutionNotFoundError, match="ACR-999"):
            engine.approve("ACR-999", "architect")
        with pytest.raises(ArchitectureEvolutionNotFoundError, match="ACR-999"):
            engine.reject("ACR-999", "no")
        with pytest.raises(ArchitectureEvolutionNotFoundError, match="ACR-999"):
            engine.apply("ACR-999")
        with pytest.raises(ArchitectureEvolutionNotFoundError, match="ACR-999"):
            engine.linked("ACR-999")


class TestApproval:
    """Approval is persisted state; nothing else can move a baseline."""

    def test_approve_records_the_human_and_accepts_the_adr(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)

        summary = engine.approve("ACR-002", "architect")

        assert summary.status is ACRStatus.APPROVED
        stored = storage.change_requests.get("ACR-002")
        assert stored is not None
        assert stored.approved_by == "architect"
        assert stored.approved_at == NOW
        assert stored.applied_at is None
        adr = storage.adrs.get(SPLIT_ADR.id)
        assert adr is not None and adr.status is ADRStatus.ACCEPTED
        transitions = tuple(
            entry.detail["status_to"]
            for entry in storage.audit.list()
            if entry.entity_type is AuditEntityType.ACR
        )
        assert transitions == ("PROPOSED", "APPROVED")

    def test_approve_is_idempotent_for_the_same_approver(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")
        audit = storage.audit.list()

        again = engine.approve("ACR-002", "architect")

        assert again.outcome is BootstrapOutcome.ALREADY_PRESENT
        assert storage.audit.list() == audit

    def test_approve_conflicts_with_a_different_approver(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")

        with pytest.raises(
            ArchitectureEvolutionConflictError, match="already APPROVED"
        ):
            engine.approve("ACR-002", "someone-else")

    def test_approve_requires_a_named_approver(self, storage, engine) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)

        with pytest.raises(ValueError, match="approved_by"):
            engine.approve("ACR-002", "   ")


class TestRejection:
    """A rejection is terminal-negative and never touches a baseline."""

    def test_reject_records_the_reason_and_touches_no_baseline(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        versions = storage.architecture_versions.list()

        summary = engine.reject("ACR-002", "not now")

        assert summary.status is ACRStatus.REJECTED
        stored = storage.change_requests.get("ACR-002")
        assert stored is not None
        assert stored.status is ACRStatus.REJECTED
        assert stored.approved_at is None
        assert stored.applied_at is None
        assert storage.architecture_versions.list() == versions
        adr = storage.adrs.get(SPLIT_ADR.id)
        assert adr is not None and adr.status is ADRStatus.REJECTED
        last = storage.audit.list()[-1]
        assert last.entity_type is AuditEntityType.ACR
        assert last.detail["reason"] == "not now"

    def test_reject_is_idempotent(self, storage, engine) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.reject("ACR-002", "not now")
        audit = storage.audit.list()

        again = engine.reject("ACR-002", "not now either")

        assert again.outcome is BootstrapOutcome.ALREADY_PRESENT
        assert again.status is ACRStatus.REJECTED
        assert storage.audit.list() == audit

    def test_a_rejected_request_is_never_revived(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.reject("ACR-002", "not now")

        with pytest.raises(
            ArchitectureEvolutionConflictError, match="never revived"
        ):
            engine.approve("ACR-002", "architect")
        with pytest.raises(
            ArchitectureEvolutionNotApprovedError, match="can never be applied"
        ):
            engine.apply("ACR-002")
        stored = storage.change_requests.get("ACR-002")
        assert stored is not None and stored.status is ACRStatus.REJECTED

    def test_reject_only_works_on_a_proposed_request(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")

        with pytest.raises(
            ArchitectureEvolutionConflictError, match="only a PROPOSED"
        ):
            engine.reject("ACR-002", "changed my mind")

    def test_an_applied_change_is_terminal(self, storage, engine) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")
        engine.apply("ACR-002")

        assert (
            engine.approve("ACR-002", "architect").status is ACRStatus.APPLIED
        )
        with pytest.raises(ArchitectureEvolutionConflictError):
            engine.reject("ACR-002", "too late")


class TestApply:
    """Only a persisted ``APPROVED`` request may move the baseline."""

    def test_a_proposed_request_can_not_be_applied(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)

        with pytest.raises(
            ArchitectureEvolutionNotApprovedError, match="requires a persisted"
        ):
            engine.apply("ACR-002")
        assert storage.architecture_versions.get(V1_2.version) is None

    def test_an_approved_request_applies_the_change(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")

        summary = engine.apply("ACR-002")

        assert summary.status is ACRStatus.APPLIED
        assert summary.outcome is BootstrapOutcome.CREATED
        assert summary.version_outcome is BootstrapOutcome.CREATED
        assert summary.applied is True
        assert summary.risks_created == ()
        stored = storage.change_requests.get("ACR-002")
        assert stored is not None
        assert stored.status is ACRStatus.APPLIED
        assert stored.applied_at == NOW
        assert stored.approved_by == "architect"
        assert engine.current_version().version == V1_2.version

    def test_apply_supersedes_without_rewriting_history(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        before = storage.architecture_versions.get(ARCHITECTURE_V1_1.version)
        assert before is not None
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")

        engine.apply("ACR-002")

        old = storage.architecture_versions.get(ARCHITECTURE_V1_1.version)
        new = storage.architecture_versions.get(V1_2.version)
        assert old is not None and new is not None
        # content is frozen: only the lifecycle moved
        assert old.baseline == before.baseline
        assert old.rules == before.rules
        assert old.created_at == before.created_at
        assert (old.is_current, old.superseded_by) == (False, V1_2.version)
        assert new.baseline == V1_2.baseline
        assert new.rules == V1_2.rules
        assert (new.is_current, new.superseded_by) == (True, None)
        versions = storage.architecture_versions.list()
        assert tuple(version.version for version in versions) == ("1.0", "1.1", "1.2")

    def test_apply_registers_the_declared_risks(
        self, storage
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        risk = RiskSpec(
            id="RISK-007",
            description="Scanner split leaves the validator without a model",
            severity=Severity.MEDIUM,
            probability=0.25,
            impact=Severity.MEDIUM,
            owner="architect",
            mitigation="The validator keeps a fallback source model.",
        )
        engine = make_engine(
            storage, extra_versions=(V1_2,), risks={"ACR-002": (risk,)}
        )
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")

        summary = engine.apply("ACR-002")

        assert summary.risks_created == ("RISK-007",)
        stored = storage.risks.get("RISK-007")
        assert stored is not None
        assert stored.description == risk.description
        assert stored.status.value == "OPEN"

    def test_apply_records_the_transition_in_the_audit_trail(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")

        engine.apply("ACR-002")

        acr_entries = tuple(
            entry
            for entry in storage.audit.list()
            if entry.entity_type is AuditEntityType.ACR
        )
        assert tuple(
            entry.detail["status_to"] for entry in acr_entries
        ) == ("PROPOSED", "APPROVED", "APPLIED")
        last = acr_entries[-1]
        assert last.detail["source_version"] == ARCHITECTURE_V1_1.version
        assert last.detail["target_version"] == V1_2.version
        assert last.detail["rules"] == list(V1_2.rules)

    def test_a_second_apply_is_a_pure_no_op(self, storage, engine) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")
        engine.apply("ACR-002")
        versions = storage.architecture_versions.list()
        audit = storage.audit.list()
        risks = storage.risks.list()
        stored = storage.change_requests.get("ACR-002")

        again = engine.apply("ACR-002")

        assert again.outcome is BootstrapOutcome.ALREADY_PRESENT
        assert again.version_outcome is BootstrapOutcome.ALREADY_PRESENT
        assert again.status is ACRStatus.APPLIED
        assert again.risks_created == ()
        assert storage.architecture_versions.list() == versions
        assert storage.audit.list() == audit
        assert storage.risks.list() == risks
        assert storage.change_requests.get("ACR-002") == stored

    """The request is state: it survives a restart, not just a call."""

class TestFailClosed:
    """A contradiction blocks the change; nothing is half-written."""

    def test_a_failed_apply_rolls_back_every_aggregate(self, storage) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        declared = RiskSpec(
            id="RISK-007",
            description="The declared description",
            severity=Severity.MEDIUM,
            probability=0.25,
            impact=Severity.MEDIUM,
            owner="architect",
            mitigation="Declared mitigation.",
        )
        # an unrelated row already squats on the declared risk id
        storage.risks.upsert(
            Risk(
                id="RISK-007",
                description="Something else entirely",
                severity=Severity.LOW,
                impact=Severity.LOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        engine = make_engine(
            storage, extra_versions=(V1_2,), risks={"ACR-002": (declared,)}
        )
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")
        versions = storage.architecture_versions.list()
        audit = storage.audit.list()
        adrs = storage.adrs.list()
        risks = storage.risks.list()

        with pytest.raises(
            ArchitectureEvolutionConflictError, match="RISK-007"
        ):
            engine.apply("ACR-002")

        # the ACR stayed approved, the old baseline stayed current and the new
        # version was never written: one transaction boundary, one outcome
        stored = storage.change_requests.get("ACR-002")
        assert stored is not None
        assert stored.status is ACRStatus.APPROVED
        assert stored.applied_at is None
        assert storage.architecture_versions.list() == versions
        assert storage.architecture_versions.get(V1_2.version) is None
        assert engine.current_version().version == ARCHITECTURE_V1_1.version
        assert storage.audit.list() == audit
        assert storage.adrs.list() == adrs
        assert storage.risks.list() == risks

    def test_a_forked_baseline_is_refused(self, storage, engine) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        storage.architecture_versions.upsert(
            replace(
                canonical_versions(NOW)[ARCHITECTURE_V1.version],
                is_current=True,
                superseded_by=None,
            )
        )
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")
        audit = storage.audit.list()

        with pytest.raises(
            ArchitectureEvolutionConflictError,
            match="current architecture versions",
        ):
            engine.apply("ACR-002")

        assert storage.architecture_versions.get(V1_2.version) is None
        assert storage.audit.list() == audit
        stored = storage.change_requests.get("ACR-002")
        assert stored is not None and stored.status is ACRStatus.APPROVED

    def test_a_version_the_code_side_never_declared_is_refused(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(target_version="9.9"), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")

        with pytest.raises(
            ArchitectureEvolutionConflictError, match="no canonical baseline"
        ):
            engine.apply("ACR-002")
        assert storage.architecture_versions.get("9.9") is None

    def test_rules_that_contradict_the_canonical_baseline_are_refused(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(rule_ids=("made-up-rule",)), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")

        with pytest.raises(
            ArchitectureEvolutionConflictError, match="declares rules"
        ):
            engine.apply("ACR-002")
        assert storage.architecture_versions.get(V1_2.version) is None

    def test_apply_requires_the_linked_adr_to_still_be_accepted(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")
        adr = storage.adrs.get(SPLIT_ADR.id)
        assert adr is not None
        storage.adrs.upsert(replace(adr, status=ADRStatus.DEPRECATED))

        with pytest.raises(
            ArchitectureEvolutionNotApprovedError, match="requires an ACCEPTED"
        ):
            engine.apply("ACR-002")
        assert storage.architecture_versions.get(V1_2.version) is None

    def test_apply_uses_the_persisted_status_not_the_caller_object(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)

        # an in-memory "approval" that was never persisted is worth nothing
        optimistic = make_request(
            status=ACRStatus.APPROVED, approved_by="architect", approved_at=NOW
        )
        assert optimistic.status is ACRStatus.APPROVED

        with pytest.raises(ArchitectureEvolutionNotApprovedError):
            engine.apply(optimistic.request_id)
        assert storage.architecture_versions.get(V1_2.version) is None

    def test_apply_has_no_bypass_parameter(self) -> None:
        parameters = tuple(
            inspect.signature(ArchitectureEvolution.apply).parameters
        )
        assert parameters == ("self", "request_id")

    def test_apply_conflicts_when_the_source_is_not_current(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(
            make_request(source_version=ARCHITECTURE_V1.version), adr=SPLIT_ADR
        )
        engine.approve("ACR-002", "architect")

        with pytest.raises(ArchitectureEvolutionConflictError):
            engine.apply("ACR-002")
        assert engine.current_version().version == ARCHITECTURE_V1_1.version


class TestPersistence:
    """The request is state: it survives a restart, not just a call."""

    def test_a_proposed_request_survives_a_restart(
        self, database_path
    ) -> None:
        with opened(database_path) as first:
            seed_history(first, current=ARCHITECTURE_V1_1.version)
            make_engine(first, extra_versions=(V1_2,)).propose(
                make_request(), adr=SPLIT_ADR
            )

        with opened(database_path) as restarted:
            reloaded = restarted.change_requests.get("ACR-002")
            assert reloaded is not None
            assert reloaded.status is ACRStatus.PROPOSED
            assert reloaded.adr_id == SPLIT_ADR.id
            assert reloaded.roadmap_impact == ("step-012",)
            assert reloaded.approved_by is None
            assert reloaded.approved_at is None
            assert reloaded.applied_at is None
            engine = make_engine(restarted, extra_versions=(V1_2,))
            assert engine.pending() == (reloaded,)
            assert engine.approved() == ()
            assert engine.applied() == ()
            assert engine.rejected() == ()

    def test_an_approved_request_survives_a_restart(
        self, database_path
    ) -> None:
        with opened(database_path) as first:
            seed_history(first, current=ARCHITECTURE_V1_1.version)
            engine = make_engine(first, extra_versions=(V1_2,))
            engine.propose(make_request(), adr=SPLIT_ADR)
            engine.approve("ACR-002", "architect")

        with opened(database_path) as restarted:
            reloaded = restarted.change_requests.get("ACR-002")
            assert reloaded is not None
            assert reloaded.status is ACRStatus.APPROVED
            assert reloaded.approved_by == "architect"
            assert reloaded.approved_at == NOW
            assert reloaded.applied_at is None
            adr = restarted.adrs.get(SPLIT_ADR.id)
            assert adr is not None and adr.status is ADRStatus.ACCEPTED
            current = restarted.architecture_versions.get(
                ARCHITECTURE_V1_1.version
            )
            assert current is not None and current.is_current is True

    def test_an_applied_request_survives_a_restart(
        self, database_path
    ) -> None:
        with opened(database_path) as first:
            seed_history(first, current=ARCHITECTURE_V1_1.version)
            engine = make_engine(first, extra_versions=(V1_2,))
            engine.propose(make_request(), adr=SPLIT_ADR)
            engine.approve("ACR-002", "architect")
            engine.apply("ACR-002")

        with opened(database_path) as restarted:
            reloaded = restarted.change_requests.get("ACR-002")
            assert reloaded is not None
            assert reloaded.status is ACRStatus.APPLIED
            assert reloaded.applied_at == NOW
            old = restarted.architecture_versions.get(
                ARCHITECTURE_V1_1.version
            )
            new = restarted.architecture_versions.get(V1_2.version)
            assert old is not None and new is not None
            assert (old.is_current, old.superseded_by) == (False, V1_2.version)
            assert (new.is_current, new.superseded_by) == (True, None)

    def test_a_rejected_request_survives_a_restart(
        self, database_path
    ) -> None:
        with opened(database_path) as first:
            seed_history(first, current=ARCHITECTURE_V1_1.version)
            engine = make_engine(first, extra_versions=(V1_2,))
            engine.propose(make_request(), adr=SPLIT_ADR)
            engine.reject("ACR-002", "too risky for now")

        with opened(database_path) as restarted:
            reloaded = restarted.change_requests.get("ACR-002")
            assert reloaded is not None
            assert reloaded.status is ACRStatus.REJECTED
            adr = restarted.adrs.get(SPLIT_ADR.id)
            assert adr is not None and adr.status is ADRStatus.REJECTED
            assert restarted.architecture_versions.get(V1_2.version) is None
            current = restarted.architecture_versions.get(
                ARCHITECTURE_V1_1.version
            )
            assert current is not None and current.is_current is True

    def test_the_whole_history_is_reloadable_in_order(
        self, database_path
    ) -> None:
        with opened(database_path) as first:
            seed_history(first, current=ARCHITECTURE_V1_1.version)
            engine = make_engine(first, extra_versions=(V1_2,))
            engine.propose(make_request(), adr=SPLIT_ADR)
            engine.propose(
                make_request(request_id="ACR-003"), adr=SPLIT_ADR
            )

        with opened(database_path) as restarted:
            engine = make_engine(restarted, extra_versions=(V1_2,))
            assert tuple(
                request.request_id for request in engine.history()
            ) == ("ACR-002", "ACR-003")


class TestStructuredLinks:
    """Links are fields, never parsed prose - the ADR text may say anything."""

    def test_the_adr_link_is_structural_not_prose(self, storage, engine) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")

        linked = engine.linked("ACR-002")

        assert linked.request.adr_id == SPLIT_ADR.id
        assert linked.adr is not None
        assert linked.adr.id == SPLIT_ADR.id
        # the prose deliberately never names the target version: the link is
        # the adr_id field, so nothing depends on the wording
        assert V1_2.version not in linked.adr.decision
        assert V1_2.version not in linked.adr.context
        assert linked.version is None

    def test_a_missing_linked_adr_is_reported_and_blocks_apply(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")
        # an approved request whose reason record has vanished: readable, but
        # not appliable
        storage.change_requests.upsert(
            replace(
                make_request(),
                status=ACRStatus.APPROVED,
                approved_by="x",
                approved_at=NOW,
                adr_id="ADR-999",
            )
        )

        linked = engine.linked("ACR-002")

        assert linked.adr is None
        assert linked.request.adr_id == "ADR-999"
        with pytest.raises(
            ArchitectureEvolutionConflictError, match="does not exist"
        ):
            engine.apply("ACR-002")
        assert storage.architecture_versions.get(V1_2.version) is None

    def test_roadmap_impact_is_a_structured_field(self, storage, engine) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        request = make_request(
            roadmap_impact=("step-012", "step-013", "step-014")
        )

        engine.propose(request, adr=SPLIT_ADR)

        stored = storage.change_requests.get("ACR-002")
        assert stored is not None
        assert stored.roadmap_impact == ("step-012", "step-013", "step-014")
        # structured means queryable, not "found by reading the rationale"
        affected = tuple(
            entry.request_id
            for entry in engine.history()
            if "step-013" in entry.roadmap_impact
        )
        assert affected == ("ACR-002",)
        assert engine.propose(request, adr=SPLIT_ADR).roadmap_impact == (
            "step-012",
            "step-013",
            "step-014",
        )


class TestDeclaredCatalogue:
    """The composition layer declares the changes; the engine owns them."""

    def test_reconcile_replays_the_declared_chain_through_the_lifecycle(
        self, storage
    ) -> None:
        seed_history(storage)  # only v1.0 is current
        engine = build_evolution(
            storage,
            clock=fixed_clock,
            now=NOW,
            versioning=ArchitectureVersioning(storage, clock=fixed_clock),
        )

        summary = reconcile_declared_changes(engine)

        assert summary.request_ids == (V1_1_ACR_ID,)
        change = change_v1_0_to_v1_1()
        assert summary.changes[0].status is ACRStatus.APPLIED
        assert summary.changes[0].outcome is BootstrapOutcome.CREATED
        assert summary.changes[0].risks_created == (
            COMPOSITION_LAYER_DRIFT_RISK.id,
        )
        assert summary.version_summary.version_outcome is (
            BootstrapOutcome.CREATED
        )
        assert summary.version_summary.superseded == ARCHITECTURE_V1.version
        stored = storage.change_requests.get(V1_1_ACR_ID)
        assert stored is not None
        assert stored.adr_id == change.adr.id
        assert stored.roadmap_impact == COMPOSITION_ROOT_ROADMAP_IMPACT
        assert stored.approved_by == DEFAULT_APPROVER
        assert engine.current_version().version == ARCHITECTURE_V1_1.version

    def test_reconcile_is_idempotent(self, storage) -> None:
        seed_history(storage)
        engine = build_evolution(storage, clock=fixed_clock, now=NOW)
        reconcile_declared_changes(engine)
        versions = storage.architecture_versions.list()
        audit = storage.audit.list()
        changes = storage.change_requests.list()
        adrs = storage.adrs.list()
        risks = storage.risks.list()

        again = reconcile_declared_changes(engine)

        assert again.changes[0].outcome is BootstrapOutcome.ALREADY_PRESENT
        assert again.changes[0].status is ACRStatus.APPLIED
        assert storage.architecture_versions.list() == versions
        assert storage.audit.list() == audit
        assert storage.change_requests.list() == changes
        assert storage.adrs.list() == adrs
        assert storage.risks.list() == risks

    def test_the_history_from_the_engine_switch_stays_intact(
        self, storage
    ) -> None:
        """A Step 9/10 database (v1.1 introduced directly) reconciles cleanly."""
        seed_history(storage)
        versioning = ArchitectureVersioning(storage, clock=fixed_clock)
        # the pre-Step-11 path: supersede directly, with no change request
        versioning.introduce(
            canonical_versions(NOW)[ARCHITECTURE_V1_1.version],
            supersedes=ARCHITECTURE_V1.version,
        )
        before = storage.architecture_versions.get(ARCHITECTURE_V1.version)
        assert before is not None
        assert storage.change_requests.list() == ()

        engine = build_evolution(
            storage, clock=fixed_clock, now=NOW, versioning=versioning
        )
        summary = reconcile_declared_changes(engine)

        # the v1.1 version transition was already in effect: no rewrite
        assert summary.version_summary.version_outcome is (
            BootstrapOutcome.ALREADY_PRESENT
        )
        after = storage.architecture_versions.get(ARCHITECTURE_V1.version)
        assert after is not None
        assert (after.baseline, after.rules, after.created_at) == (
            before.baseline,
            before.rules,
            before.created_at,
        )
        assert (after.is_current, after.superseded_by) == (
            False,
            ARCHITECTURE_V1_1.version,
        )
        # ... and the change is now recorded as authoritative state
        stored = storage.change_requests.get(V1_1_ACR_ID)
        assert stored is not None and stored.status is ACRStatus.APPLIED
        adr = storage.adrs.get(change_v1_0_to_v1_1().adr.id)
        assert adr is not None and adr.status is ADRStatus.ACCEPTED
        assert len(storage.architecture_versions.list()) == 2
        assert engine.current_version().version == ARCHITECTURE_V1_1.version

    def test_the_engine_never_deletes_a_change_request(self) -> None:
        text = (
            Path(evolution_module.__file__ or "").read_text(encoding="utf-8")
        )
        assert ".delete(" not in text
        assert "change_requests.delete" not in text

    def test_the_declarations_are_deterministic(self) -> None:
        assert declared_risks() == {V1_1_ACR_ID: (COMPOSITION_LAYER_DRIFT_RISK,)}
        assert tuple(change.request_id for change in DECLARED_CHANGES) == (
            V1_1_ACR_ID,
        )
        declared = change_v1_0_to_v1_1()
        # only the creation timestamp is allowed to differ between two calls
        assert replace(declared.request, created_at=NOW) == replace(
            DECLARED_CHANGES[0].request, created_at=NOW
        )
        assert DECLARED_CHANGES[0].adr.id == declared.adr.id
        assert DECLARED_CHANGES[0].to_dict()["adr_id"] == declared.adr.id
        assert canonical_versions(NOW)[ARCHITECTURE_V1_1.version].rules == (
            ARCHITECTURE_V1_1.rule_ids()
        )

    def test_a_change_without_roadmap_impact_is_still_complete(
        self, storage, engine
    ) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)

        summary = engine.propose(
            make_request(roadmap_impact=()), adr=SPLIT_ADR
        )

        assert summary.roadmap_impact == ()
        stored = storage.change_requests.get("ACR-002")
        assert stored is not None and stored.roadmap_impact == ()


class TestLayerBoundaries:
    """The domain change must not widen a single layer permission."""

    def test_the_engine_never_imports_the_architecture_layer(self) -> None:
        source = scan_directory(_src_root())
        module = f"{ROOT_PACKAGE}.application.architecture_evolution"
        assert module in source.modules()
        for target in source.imports_of(module):
            assert layer_of(target) not in (
                ARCHITECTURE_LAYER,
                INFRASTRUCTURE_LAYER,
                COMPOSITION_LAYER,
            ), target
            assert target != "sqlite3"

    def test_the_composition_layer_owns_the_canonical_baselines(self) -> None:
        source = scan_directory(_src_root())
        composition = source.imports_of(
            f"{ROOT_PACKAGE}.composition.evolution"
        )
        engine = source.imports_of(
            f"{ROOT_PACKAGE}.application.architecture_evolution"
        )
        # the catalogue reads the code baseline; the application engine never does
        assert f"{ROOT_PACKAGE}.architecture" in composition
        assert layer_of(f"{ROOT_PACKAGE}.architecture") == ARCHITECTURE_LAYER
        assert f"{ROOT_PACKAGE}.architecture" not in engine
        declared = canonical_versions(NOW)
        assert declared[ARCHITECTURE_V1_1.version].baseline == (
            ARCHITECTURE_V1_1.description
        )
        assert declared[ARCHITECTURE_V1.version].baseline == (
            ARCHITECTURE_V1.description
        )
        assert ARCHITECTURE_V1.version != ARCHITECTURE_V1_1.version

    def test_no_layer_gained_a_permission_for_step_11(self) -> None:
        source = scan_directory(_src_root())
        checked = 0
        for module in source.modules():
            layer = layer_of(module)
            if layer not in (APPLICATION_LAYER, "ports", "domain"):
                continue
            checked += 1
            for target in source.imports_of(module):
                assert layer_of(target) not in (
                    ARCHITECTURE_LAYER,
                    INFRASTRUCTURE_LAYER,
                    COMPOSITION_LAYER,
                ), module
        assert checked > 0

    def test_the_realized_tree_satisfies_the_current_baseline(self) -> None:
        result = ArchitectureValidator().validate(scan_directory(_src_root()))
        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
        assert ARCHITECTURE_CURRENT.version == ARCHITECTURE_V1_1.version


class TestDomainVocabulary:
    """The vocabulary grew consciously - and only by what Step 11 needs."""

    def test_the_lifecycle_has_exactly_four_statuses(self) -> None:
        assert {member.value for member in ACRStatus} == {
            "PROPOSED",
            "APPROVED",
            "REJECTED",
            "APPLIED",
        }

    def test_a_request_must_move_between_two_versions(self) -> None:
        with pytest.raises(ValueError, match="must differ"):
            make_request(target_version=ARCHITECTURE_V1_1.version)

    def test_a_request_must_declare_at_least_one_rule(self) -> None:
        with pytest.raises(ValueError, match="at least one rule id"):
            make_request(rule_ids=())

    def test_approval_fields_are_required_exactly_when_approved(self) -> None:
        with pytest.raises(ValueError, match="requires approved_by"):
            make_request(status=ACRStatus.APPROVED, approved_at=NOW)
        with pytest.raises(ValueError, match="requires approved_at"):
            make_request(status=ACRStatus.APPROVED, approved_by="architect")
        with pytest.raises(ValueError, match="must not carry approval"):
            make_request(status=ACRStatus.REJECTED, approved_by="architect")
        with pytest.raises(ValueError, match="must not carry applied_at"):
            make_request(applied_at=NOW)
        assert make_request(
            status=ACRStatus.APPLIED,
            approved_by="architect",
            approved_at=NOW,
            applied_at=NOW,
        ).is_terminal is True
        assert make_request(status=ACRStatus.REJECTED).is_terminal is True
        assert make_request().is_terminal is False

    def test_the_request_is_json_safe(self) -> None:
        payload = make_request().to_dict()
        assert payload["status"] == "PROPOSED"
        assert payload["rule_ids"] == list(V1_2.rules)
        assert payload["roadmap_impact"] == ["step-012"]
        assert payload["approved_by"] is None
        assert payload["created_at"] == NOW.isoformat()
        assert json.loads(json.dumps(payload)) == payload

    def test_the_summaries_are_json_safe(self, storage, engine) -> None:
        seed_history(storage, current=ARCHITECTURE_V1_1.version)
        engine.propose(make_request(), adr=SPLIT_ADR)
        engine.approve("ACR-002", "architect")
        summary = engine.apply("ACR-002")

        payload = summary.to_dict()
        assert payload["status"] == "APPLIED"
        assert payload["outcome"] == "created"
        assert payload["version_outcome"] == "created"
        assert payload["version_summary"]["superseded"] == (
            ARCHITECTURE_V1_1.version
        )
        assert json.loads(json.dumps(payload)) == payload
        assert engine.linked("ACR-002").to_dict()["adr"]["id"] == SPLIT_ADR.id

    def test_the_errors_share_one_base_class(self) -> None:
        for error in (
            ArchitectureEvolutionNotFoundError,
            ArchitectureEvolutionConflictError,
            ArchitectureEvolutionNotApprovedError,
        ):
            assert issubclass(error, ArchitectureEvolutionError)
        assert issubclass(ArchitectureEvolutionError, Exception)

