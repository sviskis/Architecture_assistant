"""Architecture bootstrap reconciliation.

Formalises the architectural decisions taken during Steps 1-7 as **authoritative
SQLite state** (Project, ArchitectureVersion, ADRs, Risks) and detects any
conflict between that canonical specification and what is already stored.

Design rules
------------
* The canonical :class:`ArchitectureVersion` is **injected** by the composition
  side, so this layer never duplicates the architecture rule identifiers and
  never imports the ``architecture`` layer (``application`` may only import
  ``domain`` and ``ports``).
* Reconciliation never silently overwrites anything. Every item resolves to
  **created**, **already present** (and semantically identical) or a raised
  :class:`BootstrapConflictError`. A conflict aborts the whole transaction, so
  nothing is partially committed.
* Runtime state of a live project (mode, paused flag, current-step snapshot) is
  never reset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable, Sequence

from ..domain.enums import ADRStatus, Mode, RiskStatus, Severity
from ..domain.models import ADR, ArchitectureVersion, Project, Risk
from ..ports.storage import StoragePort
from .adr_manager import ADRManager
from .risk_manager import RiskManager

__all__ = [
    "RETROSPECTIVE_NOTE",
    "PROJECT_NAME",
    "PROJECT_PLAN_VERSION",
    "BootstrapOutcome",
    "BootstrapError",
    "BootstrapConflictError",
    "AdrSpec",
    "RiskSpec",
    "BOOTSTRAP_ADRS",
    "BOOTSTRAP_RISKS",
    "BootstrapSummary",
    "ArchitectureBootstrap",
    "make_project",
    "adr_matches_spec",
    "version_matches_spec",
    "version_content_matches_spec",
    "risk_matches_spec",
]

#: Canonical project identity used when the project has to be created.
PROJECT_NAME = "Architecture Lifecycle Assistant"
PROJECT_PLAN_VERSION = "0.3"

#: Honest provenance note attached to every retrospective ADR.
RETROSPECTIVE_NOTE = (
    "Decision established during implementation Steps 1\u20137 and formally "
    "recorded during Architecture Bootstrap Reconciliation."
)


class BootstrapOutcome(StrEnum):
    """How a single bootstrap item resolved."""

    CREATED = "created"
    ALREADY_PRESENT = "already-present"


@dataclass(frozen=True)
class AdrSpec:
    """Deterministic specification of one retrospective ADR.

    ``provenance`` is the opening sentence of the ADR context. It defaults to
    :data:`RETROSPECTIVE_NOTE` (the Step 1-7 retrospective) and can be replaced
    when a later, explicitly approved architecture change records its own ADR.
    """

    id: str
    title: str
    rationale: str
    decision: str
    consequences: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    provenance: str = RETROSPECTIVE_NOTE

    @property
    def context(self) -> str:
        """The ADR context, always prefixed with the provenance sentence."""
        return f"{self.provenance} {self.rationale}"


@dataclass(frozen=True)
class RiskSpec:
    """Deterministic specification of one open risk."""

    id: str
    description: str
    severity: Severity
    probability: float
    impact: Severity
    owner: str
    mitigation: str


# ---------------------------------------------------------------------------
# the formalised decisions (Steps 1-7)
# ---------------------------------------------------------------------------

BOOTSTRAP_ADRS: tuple[AdrSpec, ...] = (
    AdrSpec(
        id="ADR-001",
        title="Dependency-free domain layer",
        rationale=(
            "The domain must stay pure state: frozen standard-library "
            "dataclasses, enums and a deterministic FSM, with no persistence, "
            "network or UI concerns."
        ),
        decision=(
            "Keep the domain dependency-free; every side effect lives behind "
            "ports implemented by outer layers."
        ),
        consequences=(
            "Domain behaviour is deterministic and trivially testable",
            "No third-party runtime dependency in the domain",
        ),
        related=("ADR-004",),
    ),
    AdrSpec(
        id="ADR-002",
        title="SQLite as the source of truth",
        rationale=(
            "State must survive restarts while staying inspectable and "
            "queryable, and exports must never become authoritative."
        ),
        decision=(
            "SQLite is the single source of truth, stored column-per-field, "
            "evolved through versioned, atomic migrations."
        ),
        consequences=(
            "Crash-safe, restart-recoverable state",
            "Exports and reports are derived views only",
        ),
        related=("ADR-005",),
    ),
    AdrSpec(
        id="ADR-003",
        title="Deterministic finite state machine",
        rationale=(
            "The Assistant <-> worker loop must be driven by explicit states and "
            "events rather than ad-hoc flags."
        ),
        decision=(
            "Model the step lifecycle as a transition table with VERIFIED and "
            "ABORTED as the only terminal states."
        ),
        consequences=(
            "Every illegal transition raises instead of silently corrupting state",
            "Retries and human overrides plug into one state vocabulary",
        ),
        related=("ADR-004",),
    ),
    AdrSpec(
        id="ADR-004",
        title="Ports and adapters with inward dependency direction",
        rationale=(
            "Storage, workers, advisors and plugins must be replaceable without "
            "touching the core."
        ),
        decision=(
            "Depend on structural Protocol ports; keep implementations in the "
            "infrastructure layer so dependencies always point inward."
        ),
        consequences=(
            "The architecture validator can enforce the dependency direction",
            "Adapters are swappable and independently testable",
        ),
        related=("ADR-005", "ADR-006"),
    ),
    AdrSpec(
        id="ADR-005",
        title="A single transaction boundary",
        rationale=(
            "Domain writes and their audit entries must commit or roll back "
            "together, without competing transaction abstractions."
        ),
        decision=(
            "One TransactionPort owns commit/rollback; StoragePort reuses it and "
            "repository adapters never commit while a boundary is active."
        ),
        consequences=(
            "Audit history can never drift from the state it describes",
            "Callers group several repository writes into one atomic unit",
        ),
        related=("ADR-002",),
    ),
    AdrSpec(
        id="ADR-006",
        title="Hybrid plugin core with explicit defaults",
        rationale=(
            "Several interchangeable adapters per capability must coexist "
            "without the registry encoding runtime policy."
        ),
        decision=(
            "The registry stores any number of named adapters per capability and "
            "keeps an explicit, deterministic default that configuration can "
            "change without re-registration."
        ),
        consequences=(
            "Worker, judge, notifier and reporting adapters stay pluggable",
            "Deterministic, testable plugin loading with no dynamic imports",
        ),
        related=("ADR-007",),
    ),
    AdrSpec(
        id="ADR-007",
        title="Cline file-channel worker adapter",
        rationale=(
            "An external worker communicates through files, so dispatch and "
            "report collection need an atomic, restart-safe channel."
        ),
        decision=(
            "Dispatch writes the context first and the attempt-scoped task file "
            "last as a commit marker; reports are read without mutation and only "
            "acknowledged after the caller durably accepted the result."
        ),
        consequences=(
            "At-least-once report delivery with explicit acknowledgement",
            "Corrupt or mismatching reports stay on disk for inspection",
        ),
        related=("ADR-006",),
    ),
)



BOOTSTRAP_RISKS: tuple[RiskSpec, ...] = (
    RiskSpec(
        id="RISK-001",
        description="Architecture drift during long automated build loops",
        severity=Severity.HIGH,
        probability=0.3,
        impact=Severity.HIGH,
        owner="architect",
        mitigation=(
            "Deterministic architecture validator plus realization control."
        ),
    ),
    RiskSpec(
        id="RISK-002",
        description="SQLite access leaking outside the infrastructure layer",
        severity=Severity.CRITICAL,
        probability=0.2,
        impact=Severity.HIGH,
        owner="architect",
        mitigation=(
            "The architecture rule that confines database imports to the "
            "infrastructure layer, enforced on every validation run."
        ),
    ),
    RiskSpec(
        id="RISK-003",
        description="Plugin core or storage facade growing into a god object",
        severity=Severity.MEDIUM,
        probability=0.4,
        impact=Severity.MEDIUM,
        owner="architect",
        mitigation=(
            "StoragePort stays a pure facade and the registry stays free of "
            "runtime policy."
        ),
    ),
    RiskSpec(
        id="RISK-004",
        description="Partially written JSON in the Cline file channel",
        severity=Severity.MEDIUM,
        probability=0.3,
        impact=Severity.MEDIUM,
        owner="architect",
        mitigation=(
            "Atomic temp-file plus os.replace writes; readers target only the "
            "final file name and ignore .tmp siblings."
        ),
    ),
    RiskSpec(
        id="RISK-005",
        description="At-least-once report delivery without de-duplication",
        severity=Severity.MEDIUM,
        probability=0.5,
        impact=Severity.MEDIUM,
        owner="architect",
        mitigation=(
            "The orchestrator provides idempotent processing and explicit "
            "acknowledgement."
        ),
    ),
)


def make_project() -> Project:
    """Build the canonical project used when none exists yet."""
    return Project(
        name=PROJECT_NAME,
        plan_version=PROJECT_PLAN_VERSION,
        mode=Mode.MANUAL,
    )


@dataclass(frozen=True)
class BootstrapSummary:
    """Outcome of a reconciliation run (conflicts are raised, never returned)."""

    project: BootstrapOutcome
    architecture_version: BootstrapOutcome
    adrs_created: tuple[str, ...] = ()
    adrs_already_present: tuple[str, ...] = ()
    risks_created: tuple[str, ...] = ()
    risks_already_present: tuple[str, ...] = ()

    @property
    def created_count(self) -> int:
        """Total number of newly created records."""
        return (
            (1 if self.project is BootstrapOutcome.CREATED else 0)
            + (
                1
                if self.architecture_version is BootstrapOutcome.CREATED
                else 0
            )
            + len(self.adrs_created)
            + len(self.risks_created)
        )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "project": self.project.value,
            "architecture_version": self.architecture_version.value,
            "adrs_created": list(self.adrs_created),
            "adrs_already_present": list(self.adrs_already_present),
            "risks_created": list(self.risks_created),
            "risks_already_present": list(self.risks_already_present),
            "created_count": self.created_count,
        }


class BootstrapError(Exception):
    """Base class for architecture bootstrap errors."""


class BootstrapConflictError(BootstrapError):
    """Raised when stored state contradicts the canonical bootstrap spec.

    The whole reconciliation transaction is rolled back, so nothing is
    partially committed.
    """

    def __init__(self, conflicts: Sequence[str]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__(
            "architecture bootstrap conflict: " + "; ".join(self.conflicts)
        )



def version_content_matches_spec(
    existing: ArchitectureVersion, spec: ArchitectureVersion
) -> bool:
    """Baseline *content* comparison - lifecycle fields are history.

    ``is_current`` / ``superseded_by`` describe where a version sits in the
    version history *now*; they are never rewritten by reconciliation. This
    comparison answers "is this the same baseline?" without touching them.
    """
    return (
        existing.version == spec.version
        and existing.baseline == spec.baseline
        and existing.rules == spec.rules
    )


def version_matches_spec(
    existing: ArchitectureVersion, spec: ArchitectureVersion
) -> bool:
    """Full semantic comparison - creation timestamps are ignored.

    Public because the minimal versioned baseline update of Step 9 reuses
    exactly this comparison; two different notions of "identical baseline" would
    be a defect.
    """
    return (
        version_content_matches_spec(existing, spec)
        and existing.superseded_by == spec.superseded_by
        and existing.is_current == spec.is_current
    )


def adr_matches_spec(existing: ADR, spec: AdrSpec) -> bool:
    """An ADR is reconciled when its content *and* its status match."""
    return (
        existing.title == spec.title
        and existing.status is ADRStatus.ACCEPTED
        and existing.context == spec.context
        and existing.decision == spec.decision
        and existing.consequences == tuple(spec.consequences)
        and existing.related == tuple(spec.related)
    )


def risk_matches_spec(existing: Risk, spec: RiskSpec) -> bool:
    """A risk is reconciled when every bootstrap field matches and it is OPEN.

    Public because the Step 11 architecture-evolution engine reuses exactly this
    comparison for the risks a change declares; two notions of "identical risk"
    would be a defect.
    """
    return (
        existing.description == spec.description
        and existing.severity is spec.severity
        and existing.probability == spec.probability
        and existing.impact is spec.impact
        and existing.owner == spec.owner
        and existing.mitigation == spec.mitigation
        and existing.status is RiskStatus.OPEN
    )


class ArchitectureBootstrap:
    """Formalises the accepted architecture as authoritative state.

    The canonical :class:`ArchitectureVersion` is **injected** through a factory,
    so the rule identifiers stay defined in exactly one place (the
    ``architecture`` layer) and this module never duplicates them.
    """

    def __init__(
        self,
        storage: StoragePort,
        architecture_version_factory: Callable[[], ArchitectureVersion],
        *,
        clock: Callable[[], datetime],
        project_factory: Callable[[], Project] = make_project,
        adr_specs: Sequence[AdrSpec] = BOOTSTRAP_ADRS,
        risk_specs: Sequence[RiskSpec] = BOOTSTRAP_RISKS,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        if not callable(architecture_version_factory):
            raise ValueError(
                "architecture_version_factory must be a callable returning an "
                "ArchitectureVersion"
            )
        if not callable(project_factory):
            raise ValueError("project_factory must be callable")
        self._storage = storage
        self._version_factory = architecture_version_factory
        self._project_factory = project_factory
        self._adr_specs = tuple(adr_specs)
        self._risk_specs = tuple(risk_specs)
        self._clock = clock
        self._adr_manager = ADRManager(
            storage.adrs, storage.audit, storage, clock=clock
        )
        self._risk_manager = RiskManager(
            storage.risks, storage.audit, storage, clock=clock
        )

    def seed(self) -> BootstrapSummary:
        """Reconcile the canonical specification with the stored state.

        Fully atomic: every item either resolves to *created* or *already
        present*, and any conflict raises :class:`BootstrapConflictError`, which
        rolls the whole transaction back so nothing is partially committed.
        """
        version_spec = self._version_factory()
        with self._storage.transaction():
            project_outcome = self._reconcile_project()
            version_outcome = self._reconcile_version(version_spec)
            adrs_created, adrs_present = self._reconcile_adrs()
            risks_created, risks_present = self._reconcile_risks()
        return BootstrapSummary(
            project=project_outcome,
            architecture_version=version_outcome,
            adrs_created=adrs_created,
            adrs_already_present=adrs_present,
            risks_created=risks_created,
            risks_already_present=risks_present,
        )


    # -- reconciliation steps --------------------------------------------
    def _reconcile_project(self) -> BootstrapOutcome:
        """Create the project only when absent; never touch live runtime state."""
        projects = self._storage.projects.list()
        spec = self._project_factory()
        if not projects:
            self._storage.projects.upsert(spec)
            return BootstrapOutcome.CREATED
        if len(projects) > 1:
            raise BootstrapConflictError(
                [
                    "expected at most one project, found "
                    f"{len(projects)}"
                ]
            )
        existing = projects[0]
        if existing.name != spec.name:
            raise BootstrapConflictError(
                [
                    f"project {existing.name!r} already exists; creating "
                    f"{spec.name!r} would violate the project singleton "
                    "invariant"
                ]
            )
        # The canonical project is present: keep its mode, paused flag,
        # current-step snapshot and plan version exactly as they are.
        return BootstrapOutcome.ALREADY_PRESENT

    def _reconcile_version(
        self, spec: ArchitectureVersion
    ) -> BootstrapOutcome:
        """Reconcile the baseline *content*; never rewrite the version history.

        A version that has legitimately become history - superseded by a newer
        version that is current - is already reconciled. That is exactly the
        approved Step 9 architecture change ``v1.0 -> v1.1``, and re-running the
        bootstrap after it must stay a no-op instead of raising a conflict.
        """
        versions = self._storage.architecture_versions.list()
        existing = self._storage.architecture_versions.get(spec.version)

        if existing is None:
            other_current = tuple(
                version for version in versions if version.is_current
            )
            if other_current:
                listed = ", ".join(
                    sorted(version.version for version in other_current)
                )
                raise BootstrapConflictError(
                    [
                        f"architecture version(s) {listed} are already "
                        "current; refusing to rewrite version history by "
                        f"making {spec.version} current"
                    ]
                )
            self._storage.architecture_versions.upsert(spec)
            return BootstrapOutcome.CREATED

        if not version_content_matches_spec(existing, spec):
            raise BootstrapConflictError(
                [
                    f"architecture version {spec.version} exists but differs "
                    "from the canonical baseline"
                ]
            )

        if existing.is_current:
            other_current = tuple(
                version
                for version in versions
                if version.is_current and version.version != spec.version
            )
            if other_current:
                listed = ", ".join(
                    sorted(version.version for version in other_current)
                )
                raise BootstrapConflictError(
                    [
                        f"architecture version(s) {listed} are already current "
                        f"alongside {spec.version}; refusing to keep two "
                        "current baselines"
                    ]
                )
            return BootstrapOutcome.ALREADY_PRESENT

        # The version is part of the recorded history: it must be superseded by
        # the version that is current now, otherwise the stored history is
        # inconsistent and must not be "repaired" silently.
        successor = self._storage.architecture_versions.get(
            existing.superseded_by or ""
        )
        if successor is None or not successor.is_current:
            raise BootstrapConflictError(
                [
                    f"architecture version {spec.version} is recorded as "
                    f"superseded by {existing.superseded_by!r}, which is not "
                    "the current baseline"
                ]
            )
        return BootstrapOutcome.ALREADY_PRESENT

    def _reconcile_adrs(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Create missing ADRs; a differing ADR is a conflict, never an amend."""
        created: list[str] = []
        present: list[str] = []
        for spec in self._adr_specs:
            existing = self._storage.adrs.get(spec.id)
            if existing is None:
                self._adr_manager.propose(
                    spec.id,
                    spec.title,
                    context=spec.context,
                    decision=spec.decision,
                    consequences=spec.consequences,
                    related=spec.related,
                )
                self._adr_manager.accept(spec.id)
                created.append(spec.id)
            elif adr_matches_spec(existing, spec):
                present.append(spec.id)
            else:
                raise BootstrapConflictError(
                    [
                        f"ADR {spec.id} exists but differs from the bootstrap "
                        "specification"
                    ]
                )
        return tuple(created), tuple(present)

    def _reconcile_risks(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Create missing risks; a differing risk is a conflict, never an amend."""
        created: list[str] = []
        present: list[str] = []
        for spec in self._risk_specs:
            existing = self._storage.risks.get(spec.id)
            if existing is None:
                self._risk_manager.register(
                    spec.id,
                    spec.description,
                    severity=spec.severity,
                    probability=spec.probability,
                    impact=spec.impact,
                    owner=spec.owner,
                    mitigation=spec.mitigation,
                )
                created.append(spec.id)
            elif risk_matches_spec(existing, spec):
                present.append(spec.id)
            else:
                raise BootstrapConflictError(
                    [
                        f"risk {spec.id} exists but differs from the bootstrap "
                        "specification"
                    ]
                )
        return tuple(created), tuple(present)

