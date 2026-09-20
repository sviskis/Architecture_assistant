"""Concrete realization check: *expected* baseline vs *actual* source tree.

This adapter is the seam where the deterministic architecture validator meets
the filesystem. It lives in the composition layer because it must import the
``architecture`` layer (scanner, validator, baseline), which the ``application``
layer may not.

Design rules
------------
* **Injected, never hardcoded.** ``source_root`` is a constructor argument with
  **no** default: the composition root decides which tree is inspected (this
  package today, some other Python/JSX repository later). Nothing in this module
  assumes the target is the assistant itself.
* **Fresh every call.** The source tree is scanned on every ``check`` - there is
  no cache and no reuse of a previous verdict.
* **Deterministic evidence.** Every violation becomes one
  :class:`~architecture_assistant.domain.models.Finding` with
  ``confidence=1.0`` (a violation is a reproducible fact about an import, not a
  guess) and the run becomes one
  :class:`~architecture_assistant.domain.models.Decision`. The decision carries
  no ``perspectives``: no advisor or judge has a vote here.
* **Stable identity.** Ids encode ``step_no`` *and* ``attempt`` plus a content
  hash, so re-validating the same code is idempotent while a different attempt
  keeps its own evidence.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Callable, Union

from ..architecture import (
    ARCHITECTURE_CURRENT,
    ArchitectureBaseline,
    ArchitectureValidator,
    RuleViolation,
    ValidationResult,
    scan_directory,
)
from ..domain.enums import DecisionStatus
from ..domain.models import Decision, Finding, utc_now
from ..ports.capabilities import RealizationCheckResult

__all__ = [
    "VALIDATOR_SOURCE",
    "ArchitectureRealizationAdapter",
]

#: Provenance stamped on every finding produced by this adapter.
VALIDATOR_SOURCE = "architecture-validator"


class ArchitectureRealizationAdapter:
    """Runs the deterministic architecture rules over the realized source."""

    def __init__(
        self,
        source_root: Union[str, Path],
        *,
        baseline: ArchitectureBaseline = ARCHITECTURE_CURRENT,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(baseline, ArchitectureBaseline):
            raise ValueError(
                f"baseline must be an ArchitectureBaseline; got {baseline!r}"
            )
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self._source_root = Path(source_root)
        self._baseline = baseline
        self._clock = clock

    @property
    def source_root(self) -> Path:
        """The source tree this adapter inspects."""
        return self._source_root

    @property
    def baseline(self) -> ArchitectureBaseline:
        """The expected architecture this adapter enforces."""
        return self._baseline

    # -- RealizationCheckPort ---------------------------------------------
    def check(self, step_no: int, attempt: int) -> RealizationCheckResult:
        """Scan ``source_root`` fresh and translate the outcome into evidence."""
        if isinstance(step_no, bool) or not isinstance(step_no, int) or step_no < 1:
            raise ValueError(f"step_no must be an int >= 1; got {step_no!r}")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError(f"attempt must be an int >= 1; got {attempt!r}")

        result = ArchitectureValidator(self._baseline).validate(
            scan_directory(self._source_root)
        )
        findings = self._findings(result, step_no, attempt)
        decision = self._decision(result, findings, step_no, attempt)
        return RealizationCheckResult(
            compliant=result.is_compliant,
            baseline_version=result.baseline_version,
            decision=decision,
            findings=findings,
        )

    # -- deterministic identity -------------------------------------------
    @staticmethod
    def finding_id(
        step_no: int, attempt: int, violation: RuleViolation
    ) -> str:
        """Content-addressed finding id (stable across repeated checks)."""
        digest = hashlib.sha1(
            "\0".join(
                (violation.rule_id, violation.module, violation.target)
            ).encode("utf-8")
        ).hexdigest()[:12]
        return f"finding-{step_no:03d}-{attempt:03d}-{digest}"

    @staticmethod
    def decision_id(step_no: int, attempt: int) -> str:
        """Decision id of one realization attempt (attempt is part of it)."""
        return f"realization-step-{step_no:03d}-attempt-{attempt:03d}"

    # -- evidence ----------------------------------------------------------
    def _findings(
        self, result: ValidationResult, step_no: int, attempt: int
    ) -> tuple[Finding, ...]:
        """One deterministic finding per rule violation."""
        now = self._clock()
        return tuple(
            Finding(
                id=self.finding_id(step_no, attempt, violation),
                source=VALIDATOR_SOURCE,
                claim=violation.rule_id,
                evidence=(
                    violation.module,
                    violation.target,
                    violation.message,
                ),
                confidence=1.0,
                severity=violation.severity,
                step_no=step_no,
                created_at=now,
            )
            for violation in result.violations
        )

    def _decision(
        self,
        result: ValidationResult,
        findings: tuple[Finding, ...],
        step_no: int,
        attempt: int,
    ) -> Decision:
        """The deterministic realization decision for one attempt.

        ``perspectives`` stays empty: this decision is produced by rules alone.
        """
        scope = (
            f"step {step_no:03d} attempt {attempt:03d} against baseline "
            f"{result.baseline_version}"
        )
        rules = ", ".join(result.checked_rules)
        if result.is_compliant:
            status = DecisionStatus.ACCEPTED
            decision = (
                "realized source satisfies architecture baseline "
                f"{result.baseline_version}"
            )
            rationale = (
                f"Fresh deterministic validation of {scope} with rules "
                f"({rules}) reported 0 violations over the scanned source tree."
            )
        else:
            status = DecisionStatus.REJECTED
            decision = (
                "realized source violates architecture baseline "
                f"{result.baseline_version}"
            )
            rationale = (
                f"Fresh deterministic validation of {scope} with rules "
                f"({rules}) reported {len(findings)} violation(s); "
                "verification must not continue."
            )
        return Decision(
            id=self.decision_id(step_no, attempt),
            status=status,
            decision=decision,
            rationale=rationale,
            rules_applied=tuple(result.checked_rules),
            evidence_refs=tuple(finding.id for finding in findings),
            perspectives=(),
            step_no=step_no,
            created_at=self._clock(),
        )
