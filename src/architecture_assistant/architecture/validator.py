"""The architecture validator: runs baseline rules over a source model."""

from __future__ import annotations

from .model import (
    ArchitectureBaseline,
    RuleViolation,
    SourceModel,
    ValidationResult,
)
from .rules import ARCHITECTURE_CURRENT

__all__ = ["ArchitectureValidator"]


class ArchitectureValidator:
    """Evaluates an architecture baseline against a source model.

    Deterministic: the rules are pure functions, so the same source model always
    yields the same, identically ordered validation result.

    By default it enforces the **current** baseline (architecture v1.1). An older
    version can still be injected explicitly to reproduce history - for example
    to show that ``composition`` was *not* legal under v1.0.
    """

    def __init__(
        self, baseline: ArchitectureBaseline = ARCHITECTURE_CURRENT
    ) -> None:
        self._baseline = baseline

    @property
    def baseline(self) -> ArchitectureBaseline:
        """The baseline being enforced."""
        return self._baseline

    def validate(self, source: SourceModel) -> ValidationResult:
        """Run every baseline rule and collect the violations."""
        violations: list[RuleViolation] = []
        for rule in self._baseline.rules:
            violations.extend(rule.check(source))
        ordered = tuple(
            sorted(violations, key=lambda violation: violation.ordering_key)
        )
        return ValidationResult(
            baseline_version=self._baseline.version,
            violations=ordered,
            checked_rules=self._baseline.rule_ids(),
        )
