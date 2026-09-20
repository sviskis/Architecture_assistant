"""Deterministic architecture rules and the versioned architecture baselines.

The rules are pure functions of a :class:`SourceModel`. They enforce the layered
architecture of the assistant itself: the allowed dependency direction between
layers, plus the rule that ``sqlite3`` may only be touched by the infrastructure
adapter. No AI, no heuristics - a violation is a reproducible fact about an
import statement.

Baseline history
----------------
* **v1.0** - the five strictly layered packages (``domain``, ``ports``,
  ``application``, ``architecture``, ``infrastructure``). Every constant of
  v1.0 is frozen here and is never edited again: the rule checks are built from
  the *frozen* v1.0 layer set and matrix, so extending the architecture can not
  silently change what v1.0 means.
* **v1.1** - adds **one** outer layer, ``composition``: the composition root,
  the only layer allowed to import ``architecture`` and ``infrastructure`` so
  that the object graph can be wired. No other layer gains any permission.

The rule identifiers stay identical across versions; the *parameters* (known
layers and allowed-import matrix) are what the version records, which is exactly
what :class:`ArchitectureVersion` persists.
"""

from __future__ import annotations

from typing import Mapping, Optional

from ..domain.enums import Severity
from .model import (
    ArchitectureBaseline,
    ArchitectureRule,
    RuleCheck,
    RuleViolation,
    SourceModel,
)

__all__ = [
    "ROOT_PACKAGE",
    "DOMAIN_LAYER",
    "PORTS_LAYER",
    "APPLICATION_LAYER",
    "ARCHITECTURE_LAYER",
    "INFRASTRUCTURE_LAYER",
    "COMPOSITION_LAYER",
    "V1_KNOWN_LAYERS",
    "V1_ALLOWED_IMPORTS",
    "KNOWN_LAYERS",
    "ALLOWED_IMPORTS",
    "SQLITE_MODULE",
    "UNKNOWN_LAYER",
    "FORBIDDEN_LAYER_IMPORT",
    "SQLITE_OUTSIDE_INFRASTRUCTURE",
    "V1_RULES",
    "V1_1_RULES",
    "ARCHITECTURE_V1",
    "ARCHITECTURE_V1_1",
    "ARCHITECTURE_BASELINES",
    "ARCHITECTURE_CURRENT",
    "layer_of",
]

#: Import prefix that identifies the assistant's own modules.
ROOT_PACKAGE = "architecture_assistant"

DOMAIN_LAYER = "domain"
PORTS_LAYER = "ports"
APPLICATION_LAYER = "application"
ARCHITECTURE_LAYER = "architecture"
INFRASTRUCTURE_LAYER = "infrastructure"

#: The outermost layer added by architecture v1.1: the composition root.
#: It is the only layer allowed to import ``architecture`` and
#: ``infrastructure``; nothing may ever import it.
COMPOSITION_LAYER = "composition"

#: The layer set of architecture v1.0 - frozen history, never extended.
V1_KNOWN_LAYERS: tuple[str, ...] = (
    DOMAIN_LAYER,
    PORTS_LAYER,
    APPLICATION_LAYER,
    ARCHITECTURE_LAYER,
    INFRASTRUCTURE_LAYER,
)

#: Allowed dependency direction of architecture v1.0 - frozen history.
#: ``layer -> layers it may import``. A layer may always import itself;
#: standard-library imports are always legal.
V1_ALLOWED_IMPORTS: dict[str, frozenset[str]] = {
    DOMAIN_LAYER: frozenset(),
    PORTS_LAYER: frozenset({DOMAIN_LAYER}),
    APPLICATION_LAYER: frozenset({DOMAIN_LAYER, PORTS_LAYER}),
    ARCHITECTURE_LAYER: frozenset({DOMAIN_LAYER}),
    INFRASTRUCTURE_LAYER: frozenset({DOMAIN_LAYER, PORTS_LAYER}),
}

#: Every top-level package inside the assistant is a layer (current baseline).
KNOWN_LAYERS: tuple[str, ...] = V1_KNOWN_LAYERS + (COMPOSITION_LAYER,)

#: Allowed dependency direction of the current baseline: the v1.0 matrix plus
#: the single new ``composition`` layer. No existing layer gains a permission.
ALLOWED_IMPORTS: dict[str, frozenset[str]] = {
    **V1_ALLOWED_IMPORTS,
    COMPOSITION_LAYER: frozenset(
        {
            DOMAIN_LAYER,
            PORTS_LAYER,
            APPLICATION_LAYER,
            ARCHITECTURE_LAYER,
            INFRASTRUCTURE_LAYER,
        }
    ),
}

#: The one third-party/system module confined to the infrastructure layer.
SQLITE_MODULE = "sqlite3"

#: Rule identifiers - identical strings are used as
#: ``ArchitectureVersion.rules`` entries when the baseline is persisted later.
UNKNOWN_LAYER = "unknown-layer"
FORBIDDEN_LAYER_IMPORT = "forbidden-layer-import"
SQLITE_OUTSIDE_INFRASTRUCTURE = "sqlite-outside-infrastructure"


def layer_of(module: str, root_package: str = ROOT_PACKAGE) -> Optional[str]:
    """Return the layer a project module belongs to.

    ``architecture_assistant.domain.models`` -> ``"domain"``
    ``architecture_assistant`` -> ``""`` (the package root itself)
    ``sqlite3``, ``typing`` -> ``None`` (not a project module)
    """
    if module == root_package:
        return ""
    prefix = root_package + "."
    if not module.startswith(prefix):
        return None
    return module[len(prefix) :].split(".", 1)[0]


# ---------------------------------------------------------------------------
# deterministic rule checks
# ---------------------------------------------------------------------------

def _make_unknown_layer_check(known_layers: tuple[str, ...]) -> RuleCheck:
    """Build the ``unknown-layer`` check for one frozen layer set."""
    known = frozenset(known_layers)

    def check(source: SourceModel) -> tuple[RuleViolation, ...]:
        """Every project module must live in a known layer."""
        violations: list[RuleViolation] = []
        for module in source.modules():
            layer = layer_of(module, source.root_package)
            if layer is None or layer == "":
                continue
            if layer not in known:
                violations.append(
                    RuleViolation(
                        rule_id=UNKNOWN_LAYER,
                        severity=Severity.HIGH,
                        module=module,
                        target=layer,
                        message=(
                            f"module {module!r} lives in unknown layer "
                            f"{layer!r}; expected one of: "
                            f"{', '.join(known_layers)}"
                        ),
                    )
                )
        return tuple(violations)

    return check


def _make_forbidden_layer_import_check(
    allowed_imports: Mapping[str, frozenset[str]],
) -> RuleCheck:
    """Build the ``forbidden-layer-import`` check for one frozen matrix."""

    def check(source: SourceModel) -> tuple[RuleViolation, ...]:
        """Imports must follow the allowed dependency direction."""
        violations: list[RuleViolation] = []
        for module in source.modules():
            source_layer = layer_of(module, source.root_package)
            if source_layer is None or source_layer == "":
                continue
            allowed = allowed_imports.get(source_layer, frozenset())
            for target in sorted(source.imports_of(module)):
                target_layer = layer_of(target, source.root_package)
                if target_layer is None or target_layer == "":
                    continue  # stdlib, third-party or the package root itself
                if target_layer == source_layer or target_layer in allowed:
                    continue
                violations.append(
                    RuleViolation(
                        rule_id=FORBIDDEN_LAYER_IMPORT,
                        severity=Severity.HIGH,
                        module=module,
                        target=target,
                        message=(
                            f"layer {source_layer!r} must not import layer "
                            f"{target_layer!r}: {module!r} imports {target!r}"
                        ),
                    )
                )
        return tuple(violations)

    return check


def _check_sqlite_outside_infrastructure(
    source: SourceModel,
) -> tuple[RuleViolation, ...]:
    """``sqlite3`` may only be imported by the infrastructure layer."""
    violations: list[RuleViolation] = []
    for module in source.modules():
        layer = layer_of(module, source.root_package)
        if layer == INFRASTRUCTURE_LAYER:
            continue
        if SQLITE_MODULE in source.imports_of(module):
            violations.append(
                RuleViolation(
                    rule_id=SQLITE_OUTSIDE_INFRASTRUCTURE,
                    severity=Severity.CRITICAL,
                    module=module,
                    target=SQLITE_MODULE,
                    message=(
                        f"{module!r} (layer {layer!r}) imports "
                        f"{SQLITE_MODULE!r}; only the "
                        f"{INFRASTRUCTURE_LAYER!r} layer may do that"
                    ),
                )
            )
    return tuple(violations)


#: ``sqlite3`` confinement rule - identical in every baseline version, so the
#: very same rule object is shared by v1.0 and v1.1.
SQLITE_CONFINEMENT_RULE: ArchitectureRule = ArchitectureRule(
    id=SQLITE_OUTSIDE_INFRASTRUCTURE,
    description=(
        f"Only the {INFRASTRUCTURE_LAYER} layer may import {SQLITE_MODULE}."
    ),
    severity=Severity.CRITICAL,
    check=_check_sqlite_outside_infrastructure,
)


def _v1_rules() -> tuple[ArchitectureRule, ...]:
    """The ordered rule set of architecture v1.0 (frozen parameters)."""
    return (
        ArchitectureRule(
            id=UNKNOWN_LAYER,
            description=(
                "Every assistant module must live in a known layer: "
                f"{', '.join(V1_KNOWN_LAYERS)}."
            ),
            severity=Severity.HIGH,
            check=_make_unknown_layer_check(V1_KNOWN_LAYERS),
        ),
        ArchitectureRule(
            id=FORBIDDEN_LAYER_IMPORT,
            description=(
                "Imports must follow the allowed dependency direction "
                "(domain <- ports <- application; architecture -> domain; "
                "infrastructure -> {domain, ports})."
            ),
            severity=Severity.HIGH,
            check=_make_forbidden_layer_import_check(V1_ALLOWED_IMPORTS),
        ),
        SQLITE_CONFINEMENT_RULE,
    )


def _v1_1_rules() -> tuple[ArchitectureRule, ...]:
    """The ordered rule set of architecture v1.1 (composition layer added)."""
    return (
        ArchitectureRule(
            id=UNKNOWN_LAYER,
            description=(
                "Every assistant module must live in a known layer: "
                f"{', '.join(KNOWN_LAYERS)}."
            ),
            severity=Severity.HIGH,
            check=_make_unknown_layer_check(KNOWN_LAYERS),
        ),
        ArchitectureRule(
            id=FORBIDDEN_LAYER_IMPORT,
            description=(
                "Imports must follow the allowed dependency direction "
                "(domain <- ports <- application; architecture -> domain; "
                "infrastructure -> {domain, ports}; composition -> every layer, "
                "and nothing may import composition)."
            ),
            severity=Severity.HIGH,
            check=_make_forbidden_layer_import_check(ALLOWED_IMPORTS),
        ),
        SQLITE_CONFINEMENT_RULE,
    )


#: The ordered rule set of architecture v1.0.
V1_RULES: tuple[ArchitectureRule, ...] = _v1_rules()

#: The ordered rule set of architecture v1.1.
V1_1_RULES: tuple[ArchitectureRule, ...] = _v1_1_rules()


#: The authoritative architecture v1.0 baseline - frozen history.
#: It keeps the *v1.0* layer set and matrix, so it never acquires the
#: ``composition`` layer retroactively.
ARCHITECTURE_V1: ArchitectureBaseline = ArchitectureBaseline(
    version="1.0",
    name="Layered Architecture Baseline",
    description=(
        "Architecture v1.0: strict layers (domain, ports, application, "
        "architecture, infrastructure) with a deterministic allowed-import "
        "matrix and sqlite3 confined to the infrastructure layer."
    ),
    known_layers=V1_KNOWN_LAYERS,
    allowed_imports={
        layer: frozenset(targets)
        for layer, targets in V1_ALLOWED_IMPORTS.items()
    },
    rules=V1_RULES,
)


#: The authoritative architecture v1.1 baseline - supersedes v1.0.
ARCHITECTURE_V1_1: ArchitectureBaseline = ArchitectureBaseline(
    version="1.1",
    name="Layered Architecture Baseline with Composition Root",
    description=(
        "Architecture v1.1: the v1.0 strict layers plus one outer composition "
        "layer (composition), which is the only layer allowed to import "
        "architecture and infrastructure. Every v1.0 permission is unchanged, "
        "and no layer may import composition."
    ),
    known_layers=KNOWN_LAYERS,
    allowed_imports={
        layer: frozenset(targets) for layer, targets in ALLOWED_IMPORTS.items()
    },
    rules=V1_1_RULES,
)

#: Every declared baseline version, oldest first - the version history.
ARCHITECTURE_BASELINES: tuple[ArchitectureBaseline, ...] = (
    ARCHITECTURE_V1,
    ARCHITECTURE_V1_1,
)

#: The baseline a validator enforces unless another one is injected.
ARCHITECTURE_CURRENT: ArchitectureBaseline = ARCHITECTURE_V1_1

