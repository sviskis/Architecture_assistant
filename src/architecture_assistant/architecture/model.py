"""Pure data types for the architecture baseline and its validation results.

Everything here is immutable, dependency-free data. The actual source scanning
lives in :mod:`architecture_assistant.architecture.scanner` and the rule
evaluation in :mod:`architecture_assistant.architecture.validator`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..domain.enums import Severity

__all__ = [
    "SourceModel",
    "RuleViolation",
    "ValidationResult",
    "ArchitectureRule",
    "ArchitectureBaseline",
    "RuleCheck",
]

#: A rule check is a pure function of the source model.
RuleCheck = Callable[["SourceModel"], tuple["RuleViolation", ...]]


@dataclass(frozen=True)
class SourceModel:
    """Derived, read-only view of the actual source structure.

    ``imports`` maps an absolute module name (for example
    ``architecture_assistant.domain.models``) to the frozenset of module names
    it imports. It is derived data: building it never touches the source of
    truth, and it is the pure input of every architecture rule.
    """

    imports: Mapping[str, frozenset[str]]
    root_package: str = "architecture_assistant"

    def modules(self) -> tuple[str, ...]:
        """All module names in deterministic (sorted) order."""
        return tuple(sorted(self.imports))

    def imports_of(self, module: str) -> frozenset[str]:
        """Imports of ``module``; an unknown module has no imports."""
        return self.imports.get(module, frozenset())

    @property
    def module_count(self) -> int:
        return len(self.imports)


@dataclass(frozen=True)
class RuleViolation:
    """One deterministic architecture violation."""

    rule_id: str
    severity: Severity
    module: str
    target: str
    message: str

    @property
    def ordering_key(self) -> tuple[str, str, str, str]:
        """Deterministic sort key."""
        return (self.rule_id, self.module, self.target, self.message)

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-safe representation."""
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "module": self.module,
            "target": self.target,
            "message": self.message,
        }


@dataclass(frozen=True)
class ValidationResult:
    """Deterministic outcome of validating a source model against a baseline."""

    baseline_version: str
    violations: tuple[RuleViolation, ...]
    checked_rules: tuple[str, ...]

    @property
    def violation_count(self) -> int:
        return len(self.violations)

    @property
    def is_compliant(self) -> bool:
        """True when no rule produced a violation."""
        return not self.violations

    def violations_for(self, rule_id: str) -> tuple[RuleViolation, ...]:
        """Violations produced by a single rule."""
        return tuple(
            violation
            for violation in self.violations
            if violation.rule_id == rule_id
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "baseline_version": self.baseline_version,
            "is_compliant": self.is_compliant,
            "violation_count": self.violation_count,
            "checked_rules": list(self.checked_rules),
            "violations": [
                violation.to_dict() for violation in self.violations
            ],
        }


@dataclass(frozen=True)
class ArchitectureRule:
    """A deterministic architecture rule.

    ``check`` must be a pure function of the source model: the same model always
    produces the same violations, so validation is fully reproducible.
    """

    id: str
    description: str
    severity: Severity
    check: RuleCheck

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("rule id must be a non-empty string")
        if not self.description or not self.description.strip():
            raise ValueError("rule description must be a non-empty string")
        if not callable(self.check):
            raise ValueError("rule check must be callable")


@dataclass(frozen=True)
class ArchitectureBaseline:
    """The authoritative, versioned architecture baseline.

    It declares the known layers, the allowed dependency direction between them
    and the ordered set of deterministic rules that enforce it.
    """

    version: str
    name: str
    description: str
    known_layers: tuple[str, ...]
    allowed_imports: Mapping[str, frozenset[str]]
    rules: tuple[ArchitectureRule, ...]

    def __post_init__(self) -> None:
        if not self.version or not self.version.strip():
            raise ValueError("baseline version must be a non-empty string")
        if not self.name or not self.name.strip():
            raise ValueError("baseline name must be a non-empty string")
        if not self.rules:
            raise ValueError("baseline must declare at least one rule")

        rule_ids = [rule.id for rule in self.rules]
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("baseline rule ids must be unique")

        for layer in self.known_layers:
            if layer not in self.allowed_imports:
                raise ValueError(
                    f"known layer {layer!r} is missing from allowed_imports"
                )
        for layer, targets in self.allowed_imports.items():
            if layer not in self.known_layers:
                raise ValueError(
                    f"allowed_imports declares unknown layer {layer!r}"
                )
            for target in targets:
                if target not in self.known_layers:
                    raise ValueError(
                        f"layer {layer!r} may not reference unknown layer "
                        f"{target!r}"
                    )

    def rule_ids(self) -> tuple[str, ...]:
        """Rule ids in declared order."""
        return tuple(rule.id for rule in self.rules)

    def rule(self, rule_id: str) -> ArchitectureRule:
        """Look up a single rule by id."""
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(f"unknown rule {rule_id!r}")

