"""Declared architecture changes - the code-side change catalogue.

Step 11 makes the architecture *change* authoritative state (a persisted
``ArchitectureChangeRequest`` with a lifecycle), but the **content** of a change
still belongs to the code baseline: which version it moves to, why, and which
risks it introduces. This module is that catalogue - the wiring input the
composition root hands to
:class:`~architecture_assistant.application.ArchitectureEvolution`.

Nothing here decides anything: the engine owns the lifecycle; this module only
declares what the project already decided, in one auditable place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from ..application import (
    COMPOSITION_ROOT_ADR,
    AdrSpec,
    ArchitectureEvolution,
    ArchitectureEvolutionError,
    ArchitectureVersioning,
    EvolutionSummary,
    RiskSpec,
    VersioningSummary,
)
from ..architecture import ARCHITECTURE_V1, ARCHITECTURE_V1_1
from ..domain.enums import Severity
from ..domain.models import ArchitectureChangeRequest, ArchitectureVersion
from ..ports.storage import StoragePort

__all__ = [
    "DEFAULT_APPROVER",
    "DeclaredChange",
    "ReconciliationSummary",
    "V1_1_ACR_ID",
    "COMPOSITION_ROOT_ROADMAP_IMPACT",
    "COMPOSITION_LAYER_DRIFT_RISK",
    "DECLARED_CHANGES",
    "canonical_versions",
    "change_v1_0_to_v1_1",
    "declared_risks",
    "build_evolution",
    "reconcile_declared_changes",
]

#: Who approves a declared change during reconciliation. The historical
#: v1.0 -> v1.1 change was approved by the human project owner in Step 9; the
#: reconciliation reconstructs that approval as *state* instead of rewriting it.
DEFAULT_APPROVER = "user"

#: Request id of the change that admitted the composition root (v1.0 -> v1.1).
V1_1_ACR_ID = "ACR-001"

#: The roadmap entries the composition-root change touched. Structured on
#: purpose, so "which steps did ACR-001 change?" is a query and not a guess.
COMPOSITION_ROOT_ROADMAP_IMPACT: tuple[str, ...] = (
    "step-009",
    "step-010",
    "step-011",
)

#: The risk the composition-root change introduces (declared, registered on
#: apply). It is new state, never a rewrite of ``BOOTSTRAP_RISKS``.
COMPOSITION_LAYER_DRIFT_RISK: RiskSpec = RiskSpec(
    id="RISK-006",
    description=(
        "The outer composition layer growing into a home for business logic, "
        "policy or loop decisions"
    ),
    severity=Severity.MEDIUM,
    probability=0.3,
    impact=Severity.MEDIUM,
    owner="architect",
    mitigation=(
        "The composition layer stays wiring and configuration only, and every "
        "earlier architecture rule keeps applying to it."
    ),
)


@dataclass(frozen=True)
class DeclaredChange:
    """One declared architecture change: the request plus its reason record."""

    request: ArchitectureChangeRequest
    adr: AdrSpec

    @property
    def request_id(self) -> str:
        """The request id, without reaching through ``request``."""
        return self.request.request_id

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "request": self.request.to_dict(),
            "adr_id": self.adr.id,
        }


@dataclass(frozen=True)
class ReconciliationSummary:
    """Outcome of replaying the declared catalogue through the lifecycle."""

    changes: tuple[EvolutionSummary, ...]

    @property
    def request_ids(self) -> tuple[str, ...]:
        """The request ids that were reconciled, in declaration order."""
        return tuple(summary.request_id for summary in self.changes)

    @property
    def version_summary(self) -> VersioningSummary:
        """The Step 9 summary of the newest change that produced one.

        Kept so the Step 9 API (``Composition.versioning_summary``) still reports
        the same thing it always did - the version outcome, what was superseded
        and which ADRs were already present - now sourced from the change
        lifecycle instead of a direct versioning call.
        """
        for summary in reversed(self.changes):
            if summary.version_summary is not None:
                return summary.version_summary
        raise ArchitectureEvolutionError(
            "no declared change produced a version summary"
        )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {"changes": [summary.to_dict() for summary in self.changes]}


def change_v1_0_to_v1_1() -> DeclaredChange:
    """The approved architecture change that admitted the composition layer.

    It supersedes v1.0 with v1.1 - the very change Step 9 performed directly and
    Step 11 now records as a first-class request, linked to ADR-008.
    """
    return DeclaredChange(
        request=ArchitectureChangeRequest(
            request_id=V1_1_ACR_ID,
            title="Admit the composition root as architecture v1.1",
            rationale=COMPOSITION_ROOT_ADR.rationale,
            source_version=ARCHITECTURE_V1.version,
            target_version=ARCHITECTURE_V1_1.version,
            rule_ids=ARCHITECTURE_V1_1.rule_ids(),
            adr_id=COMPOSITION_ROOT_ADR.id,
            roadmap_impact=COMPOSITION_ROOT_ROADMAP_IMPACT,
        ),
        adr=COMPOSITION_ROOT_ADR,
    )


#: Every architecture change the code side declares, oldest first. Reconciling
#: them in order replays the real history of the baseline.
DECLARED_CHANGES: tuple[DeclaredChange, ...] = (change_v1_0_to_v1_1(),)


def canonical_versions(now: datetime) -> Mapping[str, ArchitectureVersion]:
    """The canonical baseline records, built from the ``architecture`` layer.

    The engine receives these (never the layer itself), so it stores the exact
    baseline text and rule ids the validator enforces - and refuses a request
    that would introduce a version or a rule set the code side never declared.
    """
    return {
        ARCHITECTURE_V1.version: ArchitectureVersion(
            version=ARCHITECTURE_V1.version,
            baseline=ARCHITECTURE_V1.description,
            rules=ARCHITECTURE_V1.rule_ids(),
            is_current=True,
            created_at=now,
        ),
        ARCHITECTURE_V1_1.version: ArchitectureVersion(
            version=ARCHITECTURE_V1_1.version,
            baseline=ARCHITECTURE_V1_1.description,
            rules=ARCHITECTURE_V1_1.rule_ids(),
            is_current=True,
            created_at=now,
        ),
    }


def declared_risks(
    changes: Sequence[DeclaredChange] = DECLARED_CHANGES,
) -> Mapping[str, tuple[RiskSpec, ...]]:
    """The risks each declared change introduces, keyed by request id."""
    catalogue: dict[str, tuple[RiskSpec, ...]] = {
        change.request_id: () for change in changes
    }
    if V1_1_ACR_ID in catalogue:
        catalogue[V1_1_ACR_ID] = (COMPOSITION_LAYER_DRIFT_RISK,)
    return catalogue


def build_evolution(
    storage: StoragePort,
    *,
    clock: Callable[[], datetime],
    now: datetime,
    changes: Sequence[DeclaredChange] = DECLARED_CHANGES,
    versioning: ArchitectureVersioning | None = None,
) -> ArchitectureEvolution:
    """Wire the evolution engine over storage - wiring only, no decisions."""
    return ArchitectureEvolution(
        storage,
        canonical_versions=canonical_versions(now),
        clock=clock,
        declared_risks=declared_risks(changes),
        versioning=versioning,
    )


def reconcile_declared_changes(
    evolution: ArchitectureEvolution,
    *,
    changes: Sequence[DeclaredChange] = DECLARED_CHANGES,
    approved_by: str = DEFAULT_APPROVER,
) -> ReconciliationSummary:
    """Replay the declared catalogue as ``propose -> approve -> apply``.

    This is the only place a change is approved without an interactive human
    answer, and it exists because the historical v1.0 -> v1.1 change *was*
    approved (during Step 9): reconciliation reconstructs that approval as
    persisted state instead of rewriting the past. Every step still goes through
    the normal lifecycle - the engine's ``apply`` has no bypass parameter - and
    because each step is idempotent, a restart is a no-op.
    """
    summaries = []
    for change in changes:
        evolution.propose(change.request, adr=change.adr)
        evolution.approve(change.request.request_id, approved_by)
        summaries.append(evolution.apply(change.request.request_id))
    return ReconciliationSummary(changes=tuple(summaries))

