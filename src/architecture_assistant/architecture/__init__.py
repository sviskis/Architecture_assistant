"""Architecture baselines, deterministic rules and the architecture validator.

This layer defines *what* the architecture is and *how* it is enforced. It
depends only on ``domain`` (plus the standard library).

* :mod:`~architecture_assistant.architecture.rules` - the versioned baselines
  (v1.0 frozen history, v1.1 current, ``composition`` layer added).
* :mod:`~architecture_assistant.architecture.scanner` - builds a source model.
* :mod:`~architecture_assistant.architecture.validator` - evaluates the rules.
"""

from __future__ import annotations

from .model import (
    ArchitectureBaseline,
    ArchitectureRule,
    RuleViolation,
    SourceModel,
    ValidationResult,
)
from .rules import (
    ALLOWED_IMPORTS,
    ARCHITECTURE_BASELINES,
    ARCHITECTURE_CURRENT,
    ARCHITECTURE_LAYER,
    ARCHITECTURE_V1,
    ARCHITECTURE_V1_1,
    APPLICATION_LAYER,
    COMPOSITION_LAYER,
    DOMAIN_LAYER,
    FORBIDDEN_LAYER_IMPORT,
    INFRASTRUCTURE_LAYER,
    KNOWN_LAYERS,
    PORTS_LAYER,
    ROOT_PACKAGE,
    SQLITE_MODULE,
    SQLITE_OUTSIDE_INFRASTRUCTURE,
    UNKNOWN_LAYER,
    V1_ALLOWED_IMPORTS,
    V1_KNOWN_LAYERS,
    V1_RULES,
    V1_1_RULES,
    layer_of,
)
from .scanner import (
    build_source_model,
    discover_python_files,
    extract_imports,
    module_name_for,
    scan_directory,
)
from .validator import ArchitectureValidator

__all__ = [
    # model
    "ArchitectureBaseline",
    "ArchitectureRule",
    "RuleViolation",
    "SourceModel",
    "ValidationResult",
    # baselines
    "ARCHITECTURE_V1",
    "ARCHITECTURE_V1_1",
    "ARCHITECTURE_BASELINES",
    "ARCHITECTURE_CURRENT",
    "V1_RULES",
    "V1_1_RULES",
    "V1_KNOWN_LAYERS",
    "V1_ALLOWED_IMPORTS",
    "KNOWN_LAYERS",
    "ALLOWED_IMPORTS",
    "ROOT_PACKAGE",
    "DOMAIN_LAYER",
    "PORTS_LAYER",
    "APPLICATION_LAYER",
    "ARCHITECTURE_LAYER",
    "INFRASTRUCTURE_LAYER",
    "COMPOSITION_LAYER",
    "SQLITE_MODULE",
    "UNKNOWN_LAYER",
    "FORBIDDEN_LAYER_IMPORT",
    "SQLITE_OUTSIDE_INFRASTRUCTURE",
    "layer_of",
    # scanner
    "build_source_model",
    "discover_python_files",
    "extract_imports",
    "module_name_for",
    "scan_directory",
    # validator
    "ArchitectureValidator",
]
from .scanner import (
    build_source_model,
    discover_python_files,
    extract_imports,
    module_name_for,
    scan_directory,
)
from .validator import ArchitectureValidator

__all__ = [
    # model
    "ArchitectureBaseline",
    "ArchitectureRule",
    "RuleViolation",
    "SourceModel",
    "ValidationResult",
    # baseline
    "ARCHITECTURE_V1",
    "V1_RULES",
    "KNOWN_LAYERS",
    "ALLOWED_IMPORTS",
    "ROOT_PACKAGE",
    "DOMAIN_LAYER",
    "PORTS_LAYER",
    "APPLICATION_LAYER",
    "ARCHITECTURE_LAYER",
    "INFRASTRUCTURE_LAYER",
    "SQLITE_MODULE",
    "UNKNOWN_LAYER",
    "FORBIDDEN_LAYER_IMPORT",
    "SQLITE_OUTSIDE_INFRASTRUCTURE",
    "layer_of",
    # scanner
    "build_source_model",
    "discover_python_files",
    "extract_imports",
    "module_name_for",
    "scan_directory",
    # validator
    "ArchitectureValidator",
]
