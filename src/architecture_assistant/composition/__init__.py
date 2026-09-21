"""Composition root layer - legal since architecture v1.1.

This package is the outermost layer of the assistant: it may import every other
layer (``domain``, ``ports``, ``application``, ``architecture``,
``infrastructure``) because its single responsibility is to *build* the object
graph. No other layer may import it, and it must contain wiring and
configuration only.

It exists because of ADR-008: before v1.1 no layer was allowed to import both
``architecture`` and ``infrastructure``, so the canonical baseline could not be
assembled anywhere but in the test suite.
"""

from __future__ import annotations

from .evolution import (
    COMPOSITION_LAYER_DRIFT_RISK,
    COMPOSITION_ROOT_ROADMAP_IMPACT,
    DECLARED_CHANGES,
    DEFAULT_APPROVER,
    V1_1_ACR_ID,
    DeclaredChange,
    ReconciliationSummary,
    build_evolution,
    canonical_versions,
    change_v1_0_to_v1_1,
    declared_risks,
    reconcile_declared_changes,
)
from .root import (
    DEFAULT_REPORT_DIR,
    DEFAULT_SOURCE_ROOT,
    Composition,
    CompositionConfig,
    baseline_v1,
    baseline_v1_1,
    canonical_baseline,
    compose,
)
from .realization import (
    VALIDATOR_SOURCE,
    ArchitectureRealizationAdapter,
)
from .evidence import (
    ABSTAIN_ERRORS,
    ADVISOR_ERRORS,
    ABSTAIN_REASON_PREFIX,
    ERROR_REASON_PREFIX,
    observe,
)

__all__ = [
    "Composition",
    "CompositionConfig",
    "compose",
    "baseline_v1",
    "baseline_v1_1",
    "canonical_baseline",
    "DEFAULT_SOURCE_ROOT",
    "DEFAULT_REPORT_DIR",
    "ArchitectureRealizationAdapter",
    "VALIDATOR_SOURCE",
    # declared architecture changes (Step 11)
    "DeclaredChange",
    "ReconciliationSummary",
    "DECLARED_CHANGES",
    "V1_1_ACR_ID",
    "DEFAULT_APPROVER",
    "COMPOSITION_ROOT_ROADMAP_IMPACT",
    "COMPOSITION_LAYER_DRIFT_RISK",
    "canonical_versions",
    "change_v1_0_to_v1_1",
    "declared_risks",
    "build_evolution",
    "reconcile_declared_changes",
    # advisor observation seam (Step 15)
    "observe",
    "ABSTAIN_ERRORS",
    "ADVISOR_ERRORS",
    "ABSTAIN_REASON_PREFIX",
    "ERROR_REASON_PREFIX",
]
