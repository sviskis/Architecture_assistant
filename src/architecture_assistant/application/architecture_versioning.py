"""Minimal, deterministic versioned update of the architecture baseline.

Step 9 introduces the ``composition`` layer, which is a *real* architecture
change: the code baseline gains a new version (v1.1) and the baseline that is
already authoritative in SQLite must be **superseded**, never rewritten in
place.

This module is deliberately **not** the general architecture-evolution engine
of a later phase. It implements exactly the two primitives the approved change
needs:

* :meth:`ArchitectureVersioning.introduce` - atomically make a new
  :class:`ArchitectureVersion` current and mark its predecessor superseded,
  leaving the predecessor row (and therefore the whole version history)
  untouched;
* the accompanying ADR (:data:`COMPOSITION_ROOT_ADR`) that records *why* the
  composition root was admitted.

Both are idempotent: running them again against an already-migrated source of
truth creates nothing and writes no audit entry. A contradiction between the
stored history and the canonical specification raises
:class:`ArchitectureVersioningConflictError` and rolls the whole transaction
back - the baseline is never silently overwritten.

The canonical :class:`ArchitectureVersion` is **injected** by the composition
side, so the rule identifiers stay defined in exactly one place (the
``architecture`` layer) and this module never imports it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from ..domain.models import ArchitectureVersion
from ..ports.storage import StoragePort
from .adr_manager import ADRManager
from .architecture_bootstrap import (
    AdrSpec,
    BootstrapOutcome,
    adr_matches_spec,
    version_matches_spec,
)

__all__ = [
    "STEP_9_PROVENANCE",
    "COMPOSITION_ROOT_ADR",
    "ArchitectureVersioningError",
    "ArchitectureVersioningConflictError",
    "VersioningSummary",
    "ArchitectureVersioning",
]

#: Provenance sentence of the ADR that records the Step 9 architecture change.
STEP_9_PROVENANCE = (
    "Decision established during Step 9 (Orchestrator + Scheduler) as the "
    "approved architecture change Architecture v1.0 -> v1.1."
)


#: The ADR recording the approved admission of the composition root.
COMPOSITION_ROOT_ADR: AdrSpec = AdrSpec(
    id="ADR-008",
    title="An explicit composition root layer",
    rationale=(
        "Until now no layer was allowed to import both the 'architecture' and "
        "the 'infrastructure' layer, so the object graph could only ever be "
        "wired by the test suite. As a result the canonical baseline could not "
        "be assembled in production code and the assistant could not actually "
        "run."
    ),
    decision=(
        "Architecture v1.1 adds exactly one outer layer, 'composition', which "
        "may import domain, ports, application, architecture and "
        "infrastructure. No existing layer gains any permission: application "
        "still must not import architecture or infrastructure, and no layer may "
        "import composition. The composition layer holds wiring and "
        "configuration only - never business, policy or loop decisions."
    ),
    consequences=(
        "The baseline becomes versioned: v1.0 is kept unchanged as history and "
        "marked superseded by v1.1, which becomes the current baseline.",
        "The architecture validator enforces v1.1 by default, so the "
        "composition package is legal while every earlier rule still applies.",
        "Exactly one auditable place now owns the wiring of the adapters, the "
        "context builder and the canonical ArchitectureVersion factory.",
    ),
    related=("ADR-004", "ADR-005"),
    provenance=STEP_9_PROVENANCE,
)


class ArchitectureVersioningError(Exception):
    """Base class for baseline versioning errors."""


class ArchitectureVersioningConflictError(ArchitectureVersioningError):
    """Raised when stored baseline history contradicts the canonical spec.

    The whole transaction is rolled back, so nothing is partially committed.
    """

    def __init__(self, conflicts: Sequence[str]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__(
            "architecture versioning conflict: " + "; ".join(self.conflicts)
        )


@dataclass(frozen=True)
class VersioningSummary:
    """Deterministic outcome of one versioned baseline update."""

    version_outcome: BootstrapOutcome
    version: str
    superseded: Optional[str]
    adrs_created: tuple[str, ...] = ()
    adrs_already_present: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "version_outcome": self.version_outcome.value,
            "version": self.version,
            "superseded": self.superseded,
            "adrs_created": list(self.adrs_created),
            "adrs_already_present": list(self.adrs_already_present),
        }


class ArchitectureVersioning:
    """Introduces a new baseline version, superseding its predecessor.

    The canonical :class:`ArchitectureVersion` is injected, so this module never
    duplicates rule identifiers and never imports the ``architecture`` layer.
    """

    def __init__(
        self,
        storage: StoragePort,
        *,
        clock: Callable[[], datetime],
        adr_specs: Sequence[AdrSpec] = (COMPOSITION_ROOT_ADR,),
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self._storage = storage
        self._clock = clock
        self._adr_specs = tuple(adr_specs)
        self._adr_manager = ADRManager(
            storage.adrs, storage.audit, storage, clock=clock
        )

    def introduce(
        self, spec: ArchitectureVersion, *, supersedes: Optional[str] = None
    ) -> VersioningSummary:
        """Make ``spec`` the current baseline and record the change atomically.

        ``supersedes`` names the version that must stop being current. When it
        is omitted, the update is only allowed if no version is current yet (a
        first baseline); otherwise it is a conflict, because displacing a
        current baseline without naming it would rewrite history silently.
        """
        self._validate(spec, supersedes)
        with self._storage.transaction():
            outcome = self._roll_over(spec, supersedes)
            created, present = self._ensure_adrs()
        return VersioningSummary(
            version_outcome=outcome,
            version=spec.version,
            superseded=supersedes,
            adrs_created=created,
            adrs_already_present=present,
        )

    # -- validation ------------------------------------------------------
    @staticmethod
    def _validate(
        spec: ArchitectureVersion, supersedes: Optional[str]
    ) -> None:
        if not isinstance(spec, ArchitectureVersion):
            raise ValueError(f"spec must be an ArchitectureVersion; got {spec!r}")
        if not spec.is_current:
            raise ValueError("the introduced baseline must have is_current=True")
        if supersedes is not None and supersedes == spec.version:
            raise ValueError(
                f"a baseline cannot supersede itself ({spec.version!r})"
            )

    # -- version rollover ------------------------------------------------
    def _roll_over(
        self, spec: ArchitectureVersion, supersedes: Optional[str]
    ) -> BootstrapOutcome:
        """Insert the new version and retire exactly one predecessor."""
        existing = self._storage.architecture_versions.get(spec.version)
        if existing is not None:
            return self._already_present(existing, spec, supersedes)

        if supersedes is None:
            current = tuple(
                version
                for version in self._storage.architecture_versions.list()
                if version.is_current
            )
            if current:
                listed = ", ".join(sorted(v.version for v in current))
                raise ArchitectureVersioningConflictError(
                    [
                        f"version(s) {listed} are still current; refusing to "
                        f"introduce {spec.version} without naming the version "
                        "it supersedes"
                    ]
                )
            self._storage.architecture_versions.upsert(spec)
            return BootstrapOutcome.CREATED

        predecessor = self._storage.architecture_versions.get(supersedes)
        if predecessor is None:
            raise ArchitectureVersioningConflictError(
                [
                    "cannot supersede architecture version "
                    f"{supersedes!r}: it does not exist"
                ]
            )
        if predecessor.superseded_by is not None:
            raise ArchitectureVersioningConflictError(
                [
                    f"architecture version {predecessor.version} is already "
                    f"superseded by {predecessor.superseded_by}"
                ]
            )
        if not predecessor.is_current:
            raise ArchitectureVersioningConflictError(
                [
                    f"cannot supersede {predecessor.version}: it is not the "
                    "current baseline"
                ]
            )
        retired = replace(
            predecessor, is_current=False, superseded_by=spec.version
        )
        self._storage.architecture_versions.upsert(retired)
        self._storage.architecture_versions.upsert(spec)
        return BootstrapOutcome.CREATED

    def _already_present(
        self,
        existing: ArchitectureVersion,
        spec: ArchitectureVersion,
        supersedes: Optional[str],
    ) -> BootstrapOutcome:
        """Verify that an already-stored version matches the canonical spec."""
        if not version_matches_spec(existing, spec):
            raise ArchitectureVersioningConflictError(
                [
                    f"architecture version {spec.version} exists but differs "
                    "from the canonical baseline"
                ]
            )
        if supersedes is None:
            return BootstrapOutcome.ALREADY_PRESENT
        predecessor = self._storage.architecture_versions.get(supersedes)
        if predecessor is None:
            raise ArchitectureVersioningConflictError(
                [
                    f"architecture version {spec.version} is current, but its "
                    f"recorded predecessor {supersedes!r} is missing"
                ]
            )
        if predecessor.superseded_by != spec.version or predecessor.is_current:
            raise ArchitectureVersioningConflictError(
                [
                    f"architecture version {spec.version} is current, but "
                    f"{supersedes!r} is not recorded as superseded by it"
                ]
            )
        return BootstrapOutcome.ALREADY_PRESENT

    # -- accompanying ADRs ------------------------------------------------
    def _ensure_adrs(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Create missing change ADRs; a differing ADR is a conflict."""
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
                raise ArchitectureVersioningConflictError(
                    [
                        f"ADR {spec.id} exists but differs from the "
                        "architecture change specification"
                    ]
                )
        return tuple(created), tuple(present)
