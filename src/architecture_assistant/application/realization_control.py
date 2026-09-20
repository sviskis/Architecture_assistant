"""Architecture realization control - the fail-closed gate before ``VERIFY``.

Responsibility (deliberately narrow): answer *one* question - "does the realized
source still satisfy the authoritative architecture baseline?" - and record the
evidence for it. Nothing else lives here.

Design rules
------------
* **The persisted baseline is the runtime source of truth.** Before any source
  is inspected, the *current* :class:`ArchitectureVersion` is read from storage
  and checked against the injected canonical expectation (version + rule ids).
  A missing, duplicated or diverging baseline raises
  :class:`RealizationControlError`: no verdict is rendered, so ``VERIFY`` can
  never continue. The gate is **fail-closed**.
* **Every call validates freshly.** Stored findings/decisions are derived
  evidence only; a previously stored ``ACCEPTED`` decision is never reused as a
  compliance answer and can never stand in for a new deterministic run.
* **Identity includes the attempt.** One step may violate the baseline on
  attempt 1 and satisfy it on attempt 2; the evidence of each attempt is stored
  under its own id, so a retry never overwrites the earlier verdict.
* **Deterministic rules are the only input.** No advisor or judge takes part in
  this decision, so a violation can not be out-voted.
* The concrete check (which imports the ``architecture`` layer) sits behind
  :class:`~architecture_assistant.ports.capabilities.RealizationCheckPort`; this
  module depends on ``domain`` and ``ports`` only.
"""

from __future__ import annotations

from ..domain.models import ArchitectureVersion
from ..ports.capabilities import (
    CanonicalBaseline,
    RealizationCheckPort,
    RealizationControlPort,
    RealizationGate,
)
from ..ports.storage import StoragePort

__all__ = [
    "RealizationControlError",
    "RealizationControlUseCase",
]


class RealizationControlError(Exception):
    """Raised when no realization verdict can be reached (fail-closed).

    Raised for a control-plane inconsistency (missing, duplicated or diverging
    authoritative baseline) - the caller must never treat it as "compliant".
    """


class RealizationControlUseCase:
    """Renders and records the deterministic realization verdict of one attempt.

    Implements
    :class:`~architecture_assistant.ports.capabilities.RealizationControlPort`,
    which is what the orchestrator depends on.
    """

    def __init__(
        self,
        storage: StoragePort,
        check_port: RealizationCheckPort,
        *,
        canonical: CanonicalBaseline,
    ) -> None:
        if not isinstance(check_port, RealizationCheckPort):
            raise ValueError(
                "check_port must implement RealizationCheckPort "
                f"(check); got {type(check_port).__name__}"
            )
        if not isinstance(canonical, CanonicalBaseline):
            raise ValueError(
                f"canonical must be a CanonicalBaseline; got {canonical!r}"
            )
        self._storage = storage
        self._check = check_port
        self._canonical = canonical

    @property
    def canonical(self) -> CanonicalBaseline:
        """The code-side baseline expectation this gate enforces."""
        return self._canonical

    def gate(self, step_no: int, attempt: int) -> RealizationGate:
        """Validate the realized source for ``(step_no, attempt)`` and record it.

        The order matters and is not negotiable:

        1. the authoritative baseline read from storage must be consistent with
           the canonical one (otherwise nothing is inspected at all);
        2. the source is scanned and validated *freshly*;
        3. the evidence (findings + decision) is persisted;
        4. only then is the verdict returned.

        A previously stored verdict is never consulted.
        """
        version = self.require_authoritative_baseline()
        result = self._check.check(step_no, attempt)
        if result.baseline_version != version.version:
            raise RealizationControlError(
                "the realization check used baseline "
                f"{result.baseline_version!r}, but the authoritative baseline "
                f"is {version.version!r}"
            )
        with self._storage.transaction():
            for finding in result.findings:
                self._storage.findings.upsert(finding)
            self._storage.decisions.upsert(result.decision)
        return RealizationGate(
            compliant=result.compliant,
            step_no=step_no,
            attempt=attempt,
            baseline_version=result.baseline_version,
            violation_count=len(result.findings),
            finding_ids=tuple(finding.id for finding in result.findings),
            decision_id=result.decision.id,
        )

    # -- authoritative baseline ------------------------------------------
    def require_authoritative_baseline(self) -> ArchitectureVersion:
        """Return the single current baseline, or raise (fail-closed)."""
        versions = self._storage.architecture_versions.list()
        current = tuple(
            version for version in versions if version.is_current
        )
        if not current:
            raise RealizationControlError(
                "the source of truth holds no current architecture version; "
                "refusing to render a realization verdict"
            )
        if len(current) > 1:
            listed = ", ".join(sorted(v.version for v in current))
            raise RealizationControlError(
                "the source of truth holds "
                f"{len(current)} current architecture versions ({listed}); "
                "refusing to render a realization verdict"
            )
        version = current[0]
        if version.version != self._canonical.version:
            raise RealizationControlError(
                f"the authoritative baseline is {version.version!r}, but the "
                f"canonical baseline is {self._canonical.version!r}"
            )
        if version.rules != self._canonical.rule_ids:
            raise RealizationControlError(
                f"the authoritative baseline {version.version} persists rules "
                f"{tuple(version.rules)!r}, but the canonical baseline declares "
                f"{self._canonical.rule_ids!r}"
            )
        return version
