"""Tests for architecture realization control (the gate before ``VERIFY``).

Covered: the deterministic ``expected vs actual`` check over an *injected*
source tree, the translation of violations into Finding/Decision evidence, the
fail-closed authoritative-baseline check, fresh validation on every call, and
the attempt-scoped identity that keeps retry history intact.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import pytest

import architecture_assistant.composition.realization as realization_module
from architecture_assistant.application import (
    RealizationControlError,
    RealizationControlUseCase,
)
from architecture_assistant.architecture import (
    ARCHITECTURE_CURRENT,
    ARCHITECTURE_V1,
    ArchitectureValidator,
    scan_directory,
)
from architecture_assistant.composition import (
    VALIDATOR_SOURCE,
    ArchitectureRealizationAdapter,
    canonical_baseline,
)
from architecture_assistant.domain.enums import DecisionStatus, Severity
from architecture_assistant.domain.models import (
    ArchitectureVersion,
    Decision,
    Finding,
)
from architecture_assistant.infrastructure import (
    SqliteStorage,
    close_database,
    open_database,
)
from architecture_assistant.ports.capabilities import (
    CanonicalBaseline,
    RealizationCheckPort,
    RealizationCheckResult,
    RealizationControlPort,
)

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return NOW


#: A minimal tree that satisfies the current baseline completely.
COMPLIANT_TREE: Mapping[str, Sequence[str]] = {
    "domain/models.py": (),
    "ports/repositories.py": ("architecture_assistant.domain.models",),
    "application/context.py": (
        "architecture_assistant.domain.models",
        "architecture_assistant.ports.repositories",
    ),
    "architecture/rules.py": ("architecture_assistant.domain.enums",),
    "infrastructure/storage.py": (
        "architecture_assistant.domain.models",
        "architecture_assistant.ports.repositories",
    ),
    "composition/root.py": (
        "architecture_assistant.domain.models",
        "architecture_assistant.ports.repositories",
        "architecture_assistant.application.context",
        "architecture_assistant.architecture.rules",
        "architecture_assistant.infrastructure.storage",
    ),
}


def build_tree(
    root: Path, files: Mapping[str, Sequence[str]] | None = None
) -> Path:
    """Materialise a source tree; each file imports the given modules."""
    for relative, targets in (files or COMPLIANT_TREE).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / "__init__.py").write_text("", encoding="utf-8")
        body = "".join(f"import {target}\n" for target in targets)
        path.write_text(body, encoding="utf-8")
    return root


def adapter_for(root: Path, **kwargs) -> ArchitectureRealizationAdapter:
    kwargs.setdefault("clock", fixed_clock)
    return ArchitectureRealizationAdapter(root, **kwargs)


def seed_version(
    storage: SqliteStorage,
    *,
    version: str = ARCHITECTURE_CURRENT.version,
    is_current: bool = True,
    superseded_by: str | None = None,
    rules: Sequence[str] | None = None,
    baseline: str = "persisted baseline",
) -> ArchitectureVersion:
    record = ArchitectureVersion(
        version=version,
        baseline=baseline,
        rules=tuple(
            ARCHITECTURE_CURRENT.rule_ids() if rules is None else rules
        ),
        is_current=is_current,
        superseded_by=superseded_by,
        created_at=NOW,
    )
    storage.architecture_versions.upsert(record)
    return record


class SpyCheckPort:
    """A ``RealizationCheckPort`` that records calls and returns a canned result."""

    def __init__(self, result: RealizationCheckResult) -> None:
        self.result = result
        self.calls: list[tuple[int, int]] = []

    def check(self, step_no: int, attempt: int) -> RealizationCheckResult:
        self.calls.append((step_no, attempt))
        return self.result


def canned_result(*, compliant: bool, baseline_version: str = "1.1"):
    findings = (
        ()
        if compliant
        else (
            Finding(
                id="finding-010-001-deadbeef",
                source=VALIDATOR_SOURCE,
                claim="forbidden-layer-import",
                evidence=("application.rogue", "architecture.rules", "boom"),
                confidence=1.0,
                severity=Severity.HIGH,
                step_no=10,
                created_at=NOW,
            ),
        )
    )
    decision = Decision(
        id="realization-step-010-attempt-001",
        status=(
            DecisionStatus.ACCEPTED if compliant else DecisionStatus.REJECTED
        ),
        decision="canned decision",
        rationale="canned rationale",
        rules_applied=ARCHITECTURE_CURRENT.rule_ids(),
        evidence_refs=tuple(finding.id for finding in findings),
        step_no=10,
        created_at=NOW,
    )
    return RealizationCheckResult(
        compliant=compliant,
        baseline_version=baseline_version,
        decision=decision,
        findings=findings,
    )


@pytest.fixture
def connection():
    conn = open_database(":memory:")
    yield conn
    close_database(conn)


@pytest.fixture
def storage(connection) -> SqliteStorage:
    return SqliteStorage(connection)


@pytest.fixture
def use_case_factory(storage):
    def factory(root, **kwargs) -> RealizationControlUseCase:
        return RealizationControlUseCase(
            storage,
            adapter_for(root, **kwargs),
            canonical=canonical_baseline(),
        )

    return factory


class TestArchitectureRealizationAdapter:
    def test_compliant_tree_reports_no_violations(self, tmp_path) -> None:
        result = adapter_for(build_tree(tmp_path)).check(10, 1)

        assert result.compliant is True
        assert result.findings == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
        assert result.decision.status is DecisionStatus.ACCEPTED
        assert result.decision.rules_applied == ARCHITECTURE_CURRENT.rule_ids()
        assert result.decision.perspectives == ()
        assert result.decision.evidence_refs == ()
        assert result.decision.id == "realization-step-010-attempt-001"

    def test_expected_matches_the_validator_on_the_same_tree(
        self, tmp_path
    ) -> None:
        tree = build_tree(tmp_path)
        expected = ArchitectureValidator(ARCHITECTURE_CURRENT).validate(
            scan_directory(tree)
        )
        assert expected.is_compliant is True
        assert adapter_for(tree).check(10, 1).compliant is True

    def test_a_forbidden_import_becomes_a_finding(self, tmp_path) -> None:
        tree = build_tree(
            tmp_path,
            {
                **COMPLIANT_TREE,
                "application/rogue.py": (
                    "architecture_assistant.architecture.rules",
                ),
            },
        )

        result = adapter_for(tree).check(10, 1)

        assert result.compliant is False
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.source == VALIDATOR_SOURCE
        assert finding.claim == "forbidden-layer-import"
        assert finding.severity is Severity.HIGH
        assert finding.confidence == 1.0
        assert finding.step_no == 10
        assert finding.evidence[0] == "architecture_assistant.application.rogue"

    def test_a_dependency_violation_is_rejected_with_evidence(
        self, tmp_path
    ) -> None:
        tree = build_tree(
            tmp_path,
            {
                **COMPLIANT_TREE,
                "application/rogue.py": (
                    "architecture_assistant.architecture.rules",
                ),
            },
        )

        result = adapter_for(tree).check(10, 1)

        assert result.decision.status is DecisionStatus.REJECTED
        assert result.decision.evidence_refs == tuple(
            finding.id for finding in result.findings
        )
        assert result.decision.step_no == 10
        assert "violates architecture baseline" in result.decision.decision
        assert "must not continue" in result.decision.rationale

    def test_perspectives_stay_empty(self, tmp_path) -> None:
        """No advisor has a vote in the deterministic realization decision."""
        tree = build_tree(
            tmp_path,
            {
                **COMPLIANT_TREE,
                "application/rogue.py": (
                    "architecture_assistant.architecture.rules",
                ),
            },
        )
        result = adapter_for(tree).check(10, 1)
        assert result.decision.perspectives == ()

    def test_an_unknown_layer_is_a_violation(self, tmp_path) -> None:
        tree = build_tree(tmp_path, {**COMPLIANT_TREE, "helpers/util.py": ()})
        result = adapter_for(tree).check(10, 1)
        assert result.compliant is False
        assert {finding.claim for finding in result.findings} >= {"unknown-layer"}

    def test_sqlite_outside_infrastructure_is_a_violation(self, tmp_path) -> None:
        tree = build_tree(tmp_path, {**COMPLIANT_TREE, "domain/rogue.py": ("sqlite3",)})
        result = adapter_for(tree).check(10, 1)
        assert result.compliant is False
        severities = {f.claim: f.severity for f in result.findings}
        assert severities["sqlite-outside-infrastructure"] is Severity.CRITICAL

    def test_findings_are_content_addressed_and_stable(self, tmp_path) -> None:
        tree = build_tree(
            tmp_path,
            {
                **COMPLIANT_TREE,
                "application/rogue.py": (
                    "architecture_assistant.architecture.rules",
                ),
            },
        )
        adapter = adapter_for(tree)

        first = adapter.check(10, 1)
        second = adapter.check(10, 1)

        assert [f.id for f in first.findings] == [f.id for f in second.findings]
        assert first.findings == second.findings

    def test_identity_encodes_step_and_attempt(self, tmp_path) -> None:
        assert (
            ArchitectureRealizationAdapter.decision_id(10, 2)
            == "realization-step-010-attempt-002"
        )
        assert ArchitectureRealizationAdapter.decision_id(
            10, 1
        ) != ArchitectureRealizationAdapter.decision_id(10, 2)

    def test_each_call_scans_fresh(self, tmp_path) -> None:
        tree = build_tree(tmp_path)
        adapter = adapter_for(tree)
        assert adapter.check(10, 1).compliant is True

        (tree / "application" / "rogue.py").write_text(
            "import architecture_assistant.architecture.rules\n",
            encoding="utf-8",
        )

        after = adapter.check(10, 1)
        assert after.compliant is False
        assert after.findings

    def test_source_root_is_injectable(self, tmp_path) -> None:
        good = build_tree(tmp_path / "good")
        bad = build_tree(
            tmp_path / "bad",
            {
                **COMPLIANT_TREE,
                "application/rogue.py": (
                    "architecture_assistant.architecture.rules",
                ),
            },
        )

        assert adapter_for(good).check(10, 1).compliant is True
        assert adapter_for(bad).check(10, 1).compliant is False
        assert adapter_for(bad).source_root == bad
        assert adapter_for(good).source_root != adapter_for(bad).source_root

    def test_the_adapter_does_not_hardcode_a_target_path(self) -> None:
        text = Path(realization_module.__file__ or "").read_text(encoding="utf-8")
        assert "src/architecture_assistant" not in text
        assert "src\\architecture_assistant" not in text

    def test_source_root_has_no_default(self) -> None:
        """The target tree is always supplied by the composition root."""
        import inspect

        signature = inspect.signature(ArchitectureRealizationAdapter.__init__)
        parameter = signature.parameters["source_root"]
        assert parameter.default is inspect.Parameter.empty

    def test_the_adapter_conforms_to_the_check_port(self, tmp_path) -> None:
        assert isinstance(adapter_for(tmp_path), RealizationCheckPort)

    def test_result_is_json_safe(self, tmp_path) -> None:
        result = adapter_for(build_tree(tmp_path)).check(10, 1)
        assert json.loads(json.dumps(result.to_dict())) == result.to_dict()

    def test_baseline_must_be_a_baseline(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="ArchitectureBaseline"):
            ArchitectureRealizationAdapter(tmp_path, baseline="1.1")

    def test_clock_must_be_callable(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="clock must be a callable"):
            ArchitectureRealizationAdapter(tmp_path, clock="now")

    def test_step_and_attempt_are_validated(self, tmp_path) -> None:
        adapter = adapter_for(tmp_path)
        with pytest.raises(ValueError, match="step_no must be an int"):
            adapter.check(0, 1)
        with pytest.raises(ValueError, match="attempt must be an int"):
            adapter.check(10, 0)


def violating_tree(root: Path) -> Path:
    """A tree whose application layer reaches into the architecture layer."""
    return build_tree(
        root,
        {
            **COMPLIANT_TREE,
            "application/rogue.py": (
                "architecture_assistant.architecture.rules",
            ),
        },
    )


class CountingCheckPort:
    """Wraps a check port and records that it was asked (fresh validation)."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[tuple[int, int]] = []

    def check(self, step_no: int, attempt: int) -> RealizationCheckResult:
        self.calls.append((step_no, attempt))
        return self._inner.check(step_no, attempt)


class ExplodingFindings:
    """A findings repository whose writes always fail."""

    def upsert(self, finding: Finding) -> None:
        raise RuntimeError("findings storage is down")

    def get(self, finding_id: str):
        return None

    def list(self) -> tuple[Finding, ...]:
        return ()

    def delete(self, finding_id: str) -> bool:
        return False


class TestRealizationControlUseCase:
    def test_a_compliant_gate_persists_an_accepted_decision(
        self, storage, use_case_factory, tmp_path
    ) -> None:
        seed_version(storage)

        gate = use_case_factory(build_tree(tmp_path)).gate(10, 1)

        assert gate.compliant is True
        assert gate.violation_count == 0
        assert gate.finding_ids == ()
        assert gate.baseline_version == ARCHITECTURE_CURRENT.version
        assert gate.decision_id == "realization-step-010-attempt-001"
        decisions = storage.decisions.list()
        assert len(decisions) == 1
        assert decisions[0].status is DecisionStatus.ACCEPTED
        assert storage.findings.list() == ()

    def test_a_violation_gate_persists_findings_and_a_rejection(
        self, storage, use_case_factory, tmp_path
    ) -> None:
        seed_version(storage)

        gate = use_case_factory(violating_tree(tmp_path)).gate(10, 1)

        assert gate.compliant is False
        assert gate.violation_count == len(gate.finding_ids) == 1
        stored_findings = storage.findings.list()
        assert [finding.id for finding in stored_findings] == list(gate.finding_ids)
        decision = storage.decisions.get(gate.decision_id)
        assert decision is not None
        assert decision.status is DecisionStatus.REJECTED
        assert decision.evidence_refs == gate.finding_ids

    def test_the_gate_is_repeatable_for_the_same_attempt(
        self, storage, use_case_factory, tmp_path
    ) -> None:
        seed_version(storage)
        use_case = use_case_factory(violating_tree(tmp_path))

        first = use_case.gate(10, 1)
        second = use_case.gate(10, 1)

        assert first == second
        assert len(storage.decisions.list()) == 1
        assert len(storage.findings.list()) == 1

    def test_attempt_identity_keeps_every_attempt_verdict(
        self, storage, use_case_factory, tmp_path
    ) -> None:
        """attempt 1 violated, attempt 2 is compliant - both stay in history."""
        seed_version(storage)

        first = use_case_factory(violating_tree(tmp_path / "attempt1")).gate(10, 1)
        second = use_case_factory(build_tree(tmp_path / "attempt2")).gate(10, 2)

        assert first.compliant is False
        assert second.compliant is True
        decisions = {d.id: d for d in storage.decisions.list()}
        assert decisions["realization-step-010-attempt-001"].status is (
            DecisionStatus.REJECTED
        )
        assert decisions["realization-step-010-attempt-002"].status is (
            DecisionStatus.ACCEPTED
        )
        assert first.decision_id != second.decision_id
        # the earlier attempt's findings are not rewritten as accepted
        assert any(
            finding.id.startswith("finding-010-001-")
            for finding in storage.findings.list()
        )

    def test_gate_is_json_safe(self, storage, use_case_factory, tmp_path) -> None:
        seed_version(storage)
        gate = use_case_factory(build_tree(tmp_path)).gate(10, 1)
        assert json.loads(json.dumps(gate.to_dict())) == gate.to_dict()


class TestFailClosedBaseline:
    def test_no_current_baseline_fails_closed(self, storage) -> None:
        spy = SpyCheckPort(canned_result(compliant=True))
        use_case = RealizationControlUseCase(
            storage, spy, canonical=canonical_baseline()
        )

        with pytest.raises(RealizationControlError, match="no current"):
            use_case.gate(10, 1)

        assert spy.calls == []  # nothing was inspected at all

    def test_multiple_current_baselines_fail_closed(self, storage) -> None:
        seed_version(storage, version="1.1")
        seed_version(storage, version="1.2")
        spy = SpyCheckPort(canned_result(compliant=True))
        use_case = RealizationControlUseCase(
            storage, spy, canonical=canonical_baseline()
        )

        with pytest.raises(RealizationControlError, match="2 current"):
            use_case.gate(10, 1)

        assert spy.calls == []

    def test_diverging_persisted_version_fails_closed(self, storage) -> None:
        seed_version(storage, version="9.9")
        spy = SpyCheckPort(canned_result(compliant=True, baseline_version="9.9"))
        use_case = RealizationControlUseCase(
            storage, spy, canonical=canonical_baseline()
        )

        with pytest.raises(RealizationControlError, match="canonical baseline"):
            use_case.gate(10, 1)

        assert spy.calls == []

    def test_diverging_persisted_rules_fail_closed(self, storage) -> None:
        seed_version(storage, rules=("a-different-rule",))
        spy = SpyCheckPort(canned_result(compliant=True))
        use_case = RealizationControlUseCase(
            storage, spy, canonical=canonical_baseline()
        )

        with pytest.raises(RealizationControlError, match="rules"):
            use_case.gate(10, 1)

        assert spy.calls == []

    def test_a_check_against_a_different_baseline_fails_closed(
        self, storage, tmp_path
    ) -> None:
        seed_version(storage)
        adapter = adapter_for(build_tree(tmp_path), baseline=ARCHITECTURE_V1)
        use_case = RealizationControlUseCase(
            storage, adapter, canonical=canonical_baseline()
        )

        with pytest.raises(RealizationControlError, match="the realization check"):
            use_case.gate(10, 1)

    def test_the_baseline_is_readable_without_gating(
        self, storage, tmp_path
    ) -> None:
        seed_version(storage)
        use_case = RealizationControlUseCase(
            storage,
            adapter_for(build_tree(tmp_path)),
            canonical=canonical_baseline(),
        )
        assert use_case.require_authoritative_baseline().version == (
            ARCHITECTURE_CURRENT.version
        )
        assert use_case.canonical.version == ARCHITECTURE_CURRENT.version


class TestFreshValidation:
    def test_a_stored_accepted_decision_never_replaces_a_fresh_scan(
        self, storage, tmp_path
    ) -> None:
        """Derived evidence is never the compliance source of truth."""
        seed_version(storage)
        storage.decisions.upsert(
            Decision(
                id="realization-step-010-attempt-001",
                status=DecisionStatus.ACCEPTED,
                decision="an earlier run accepted this",
                rationale="stale evidence",
                created_at=NOW,
            )
        )

        counting = CountingCheckPort(adapter_for(violating_tree(tmp_path)))
        use_case = RealizationControlUseCase(
            storage, counting, canonical=canonical_baseline()
        )

        gate = use_case.gate(10, 1)

        assert counting.calls == [(10, 1)]  # the port was asked again
        assert gate.compliant is False  # the stale ACCEPTED did not help
        stored = storage.decisions.get("realization-step-010-attempt-001")
        assert stored is not None
        assert stored.status is DecisionStatus.REJECTED
        assert stored.rationale != "stale evidence"

    def test_persistence_failure_rolls_back_and_raises(
        self, storage, use_case_factory, tmp_path, monkeypatch
    ) -> None:
        seed_version(storage)
        use_case = use_case_factory(violating_tree(tmp_path))
        monkeypatch.setattr(storage, "_findings", ExplodingFindings())

        with pytest.raises(RuntimeError, match="findings storage is down"):
            use_case.gate(10, 1)

        assert storage.decisions.list() == ()
        assert storage.findings.list() == ()

    def test_the_use_case_conforms_to_the_control_port(
        self, storage, use_case_factory, tmp_path
    ) -> None:
        assert isinstance(
            use_case_factory(build_tree(tmp_path)), RealizationControlPort
        )

    def test_check_port_is_validated(self, storage) -> None:
        with pytest.raises(ValueError, match="RealizationCheckPort"):
            RealizationControlUseCase(
                storage, object(), canonical=canonical_baseline()
            )

    def test_canonical_is_validated(self, storage, tmp_path) -> None:
        with pytest.raises(ValueError, match="CanonicalBaseline"):
            RealizationControlUseCase(
                storage, adapter_for(tmp_path), canonical="1.1"
            )

    def test_domain_evidence_models_are_not_extended(self) -> None:
        """Step 10 must not silently widen the Step 1 domain models."""
        assert {f.name for f in fields(Finding)} == {
            "id",
            "source",
            "claim",
            "evidence",
            "confidence",
            "severity",
            "step_no",
            "created_at",
        }
        assert {f.name for f in fields(Decision)} == {
            "id",
            "status",
            "decision",
            "rationale",
            "rules_applied",
            "evidence_refs",
            "perspectives",
            "step_no",
            "created_at",
        }

    def test_only_existing_enum_values_are_used(self) -> None:
        assert DecisionStatus.ACCEPTED.value == "ACCEPTED"
        assert DecisionStatus.REJECTED.value == "REJECTED"
        assert {member.value for member in Severity} == {
            "LOW",
            "MEDIUM",
            "HIGH",
            "CRITICAL",
        }

    def test_canonical_baseline_rejects_empty_rules(self) -> None:
        with pytest.raises(ValueError, match="at least one rule id"):
            CanonicalBaseline(version="1.1", rule_ids=())
