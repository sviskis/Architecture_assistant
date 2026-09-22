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
    ACTION_CANCEL,
    ACTION_GENERATE_FINAL_SYNTHESIS,
    ACTION_GENERATE_LEAD_REVIEW,
    ACTION_GENERATE_PROPOSAL,
    ACTION_RUN_ROUND1,
    ACTION_RUN_ROUND2,
    DEFAULT_REPORT_DIR,
    DEFAULT_REVIEW_QUESTION,
    DEFAULT_SOURCE_ROOT,
    DELIBERATION_ACTIONS,
    apply_provider_settings,
    EVENT_LEVEL_ERROR,
    EVENT_LEVEL_INFO,
    EVENT_LEVEL_WARN,
    EVENT_LEVELS,
    LOG_COMPONENTS,
    Composition,
    CompositionConfig,
    baseline_v1,
    baseline_v1_1,
    build_event,
    canonical_baseline,
    component_for,
    compose,
    exception_reason,
    sanitize_text,
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
from ..ports.capabilities import ConnectionStatus
from .advisor_factory import (
    PROVIDER_DEFAULT_MODELS,
    AdvisorFactory,
    assemble_deliberation,
    assemble_reviewers,
    create_deliberation_agent,
    create_deliberation_lead,
    default_model_for,
    probe_connection,
    probe_deliberation,
    provider_catalog,
)
from .provider_settings import (
    ADVISOR_KEYS,
    DEFAULT_PROVIDER_BY_ADVISOR,
    DEFAULT_PROVIDER_SETTINGS_PATH,
    MASKED_KEY,
    PROVIDER_LABELS,
    PROVIDER_SETTINGS_FILENAME,
    SETTINGS_STATUS_DEFAULTS,
    SETTINGS_STATUS_INVALID,
    SETTINGS_STATUS_LOADED,
    SETTINGS_STATUS_TEXTS,
    SUPPORTED_PROVIDERS,
    ProviderSelection,
    ProviderSettings,
    ProviderSettingsLoad,
    default_provider_settings,
    load_provider_settings,
    save_provider_settings,
    settings_from_mapping,
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
    "DEFAULT_REVIEW_QUESTION",
    # the deliberation lifecycle verbs the panel offers (Step 29)
    "DELIBERATION_ACTIONS",
    "ACTION_RUN_ROUND1",
    "ACTION_GENERATE_LEAD_REVIEW",
    "ACTION_RUN_ROUND2",
    "ACTION_GENERATE_FINAL_SYNTHESIS",
    "ACTION_GENERATE_PROPOSAL",
    "ACTION_CANCEL",
    "apply_provider_settings",
    # the log-event contract (Step 27)
    "EVENT_LEVELS",
    "EVENT_LEVEL_INFO",
    "EVENT_LEVEL_WARN",
    "EVENT_LEVEL_ERROR",
    "LOG_COMPONENTS",
    "build_event",
    "component_for",
    "exception_reason",
    "sanitize_text",
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
    # the provider vocabulary shared by the factory and the panel
    "ConnectionStatus",
    # the composition-level advisor factory
    "AdvisorFactory",
    "PROVIDER_DEFAULT_MODELS",
    "assemble_reviewers",
    "default_model_for",
    "probe_connection",
    "provider_catalog",
    # the deliberation seats and chair (Step 29)
    "assemble_deliberation",
    "create_deliberation_agent",
    "create_deliberation_lead",
    "probe_deliberation",
    # per-advisor provider settings (which provider/model/key each slot uses)
    "ADVISOR_KEYS",
    "DEFAULT_PROVIDER_BY_ADVISOR",
    "DEFAULT_PROVIDER_SETTINGS_PATH",
    "MASKED_KEY",
    "PROVIDER_LABELS",
    "PROVIDER_SETTINGS_FILENAME",
    "SETTINGS_STATUS_DEFAULTS",
    "SETTINGS_STATUS_INVALID",
    "SETTINGS_STATUS_LOADED",
    "SETTINGS_STATUS_TEXTS",
    "SUPPORTED_PROVIDERS",
    "ProviderSelection",
    "ProviderSettings",
    "ProviderSettingsLoad",
    "default_provider_settings",
    "load_provider_settings",
    "save_provider_settings",
    "settings_from_mapping",
]
