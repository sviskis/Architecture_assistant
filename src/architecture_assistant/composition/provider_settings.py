"""Per-advisor provider settings: which provider/model each advisor uses, and its key.

This module is the one place that knows the *shape* of the operator's provider
configuration, how it is loaded and how it is saved. It is deliberately a
composition-layer concern - the GUI (an external host) and the composition root
both need it, and it is neither domain state nor a provider adapter.

Three rules shape it, and they are the same three that shape the host's
remembered window layout (a sibling local-preference file the panel owns):

* **a local preference, never domain state.** The file lives in the assistant's
  runtime ``data/`` directory (ignored by Git, like the default database beside
  it). It is never written to SQLite, never audited, never rendered into a report
  and never part of a ``Finding``, a ``Decision`` or an
  ``ArchitectureReviewResult``;
* **fail-safe.** A missing file means "use the documented defaults"; a malformed
  file means "use the defaults and say so". Nothing in this module can raise into
  the GUI, and a damaged file can never reach the workflow;
* **secret-safe by construction.** An API key is a field of exactly one file and
  is never rendered: :meth:`ProviderSelection.__repr__` masks it, ``to_view()``
  reports only whether one is set, and every error message names a *field*, never
  a value. Nothing here prints, logs or formats a key.

Where the key lives
-------------------
``data/provider_settings.json`` is a **plain-text local-only** store. That is a
deliberate, temporary trade documented in the README: the file is Git-ignored and
lives beside the operator's own database, and the whole thing is isolated behind
:class:`ProviderSelection`/:class:`ProviderSettings` so a future
credential-store backend (for example the Windows Credential Manager) can
replace the key field without touching the GUI, the factory or an adapter.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
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

#: The file name of the remembered provider configuration.
PROVIDER_SETTINGS_FILENAME = "provider_settings.json"

#: Where it lives: the assistant's runtime data directory, already Git-ignored
#: (``data/`` in ``.gitignore``), beside the default database and the layout.
DEFAULT_PROVIDER_SETTINGS_PATH: Path = Path("data") / PROVIDER_SETTINGS_FILENAME

#: The three advisor slots the panel offers, in the order it shows them. They are
#: slot *names*, never provider names: which provider a slot uses is the whole
#: point of this module.
ADVISOR_KEYS: tuple[str, ...] = ("advisor_1", "advisor_2", "advisor_3")

#: Every provider the configuration accepts. ``disabled`` is a real choice - "run
#: this slot without any provider" - and not a placeholder for a missing one.
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "openai",
    "claude",
    "grok",
    "deepseek",
    "disabled",
)

#: How each provider is shown in the panel.
PROVIDER_LABELS: dict[str, str] = {
    "openai": "OpenAI",
    "claude": "Claude",
    "grok": "Grok",
    "deepseek": "DeepSeek",
    "disabled": "Disabled",
}

#: The default provider of each slot, used when no file exists. These are the
#: assistant's established defaults (implementation analyst, critical reviewer,
#: challenger), kept exactly as they were before this file existed.
DEFAULT_PROVIDER_BY_ADVISOR: dict[str, str] = {
    "advisor_1": "openai",
    "advisor_2": "claude",
    "advisor_3": "grok",
}

#: The three load outcomes the panel can report.
SETTINGS_STATUS_LOADED = "loaded"
SETTINGS_STATUS_DEFAULTS = "defaults"
SETTINGS_STATUS_INVALID = "invalid"

#: The operator-facing sentence for each outcome. ``invalid`` is the one the task
#: requires verbatim.
SETTINGS_STATUS_TEXTS: dict[str, str] = {
    SETTINGS_STATUS_LOADED: "provider settings loaded",
    SETTINGS_STATUS_DEFAULTS: "provider settings missing; using defaults",
    SETTINGS_STATUS_INVALID: "provider settings invalid",
}

#: What the panel shows in place of a stored key. The key itself is never handed
#: to a widget, a view model, a log entry or a test assertion.
MASKED_KEY = "********"


@dataclass(frozen=True)
class ProviderSelection:
    """Which provider one advisor slot uses, with which model and key."""

    provider: str = "disabled"
    model: str = ""
    api_key: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str):
            raise ValueError("provider must be a string")
        provider = self.provider.strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                "provider must be one of "
                f"{', '.join(SUPPORTED_PROVIDERS)}; got {self.provider!r}"
            )
        object.__setattr__(self, "provider", provider)
        if not isinstance(self.model, str):
            raise ValueError("model must be a string")
        object.__setattr__(self, "model", self.model.strip())
        if not isinstance(self.api_key, str):
            # never the value: a key must not be able to reach a message
            raise ValueError("api_key must be a string")
        # a blank key is "no key stored", exactly as the adapters treat it
        object.__setattr__(self, "api_key", self.api_key.strip())

    #: A key is a secret: it must never be rendered, printed or logged.
    def __repr__(self) -> str:
        """Redacted on purpose - the API key is never part of a repr."""
        return (
            f"ProviderSelection(provider={self.provider!r}, "
            f"model={self.model!r}, "
            f"api_key={'set' if self.api_key else 'unset'})"
        )

    __str__ = __repr__

    @property
    def key_set(self) -> bool:
        """Whether a key is stored - the only fact a view is allowed to show."""
        return bool(self.api_key)

    def to_file_dict(self) -> dict[str, str]:
        """The full record, **including** the key - for the settings file only.

        This is the one method that carries the secret, and it exists for exactly
        one caller: :func:`save_provider_settings`. It must never be used for a
        log line, an audit detail, a report or a view model.
        """
        return {
            "provider": self.provider,
            "model": self.model,
            "api_key": self.api_key,
        }

    def to_view(self) -> dict[str, Any]:
        """A key-free view: the provider, the model and whether a key is set."""
        return {
            "provider": self.provider,
            "provider_label": PROVIDER_LABELS.get(self.provider, self.provider),
            "model": self.model,
            "key_set": self.key_set,
            "key_masked": MASKED_KEY if self.key_set else "",
        }


@dataclass(frozen=True)
class ProviderSettings:
    """One :class:`ProviderSelection` per advisor slot, in the panel's order."""

    advisor_1: ProviderSelection = ProviderSelection("openai")
    advisor_2: ProviderSelection = ProviderSelection("claude")
    advisor_3: ProviderSelection = ProviderSelection("grok")

    def __repr__(self) -> str:
        """Redacted through the selections, which never render a key."""
        return (
            "ProviderSettings("
            + ", ".join(f"{key}={getattr(self, key)!r}" for key in ADVISOR_KEYS)
            + ")"
        )

    __str__ = __repr__

    def selection(self, key: str) -> ProviderSelection:
        """The selection of one advisor slot, or ``KeyError`` for a bad slot."""
        if key not in ADVISOR_KEYS:
            raise KeyError(key)
        return getattr(self, key)

    def with_selection(
        self, key: str, selection: ProviderSelection
    ) -> "ProviderSettings":
        """A copy with one slot replaced - the value objects stay immutable."""
        if key not in ADVISOR_KEYS:
            raise KeyError(key)
        if not isinstance(selection, ProviderSelection):
            raise ValueError("selection must be a ProviderSelection")
        return ProviderSettings(
            **{
                slot: selection if slot == key else getattr(self, slot)
                for slot in ADVISOR_KEYS
            }
        )

    def to_file_dict(self) -> dict[str, dict[str, str]]:
        """The full record - secrets included - for the settings file only."""
        return {key: getattr(self, key).to_file_dict() for key in ADVISOR_KEYS}

    def to_view(self) -> dict[str, dict[str, Any]]:
        """The key-free view of every slot, for the panel and the core payload."""
        return {key: getattr(self, key).to_view() for key in ADVISOR_KEYS}


@dataclass(frozen=True)
class ProviderSettingsLoad:
    """What loading the file produced: the settings and how they were obtained."""

    settings: ProviderSettings
    status: str

    def __post_init__(self) -> None:
        if not isinstance(self.settings, ProviderSettings):
            raise ValueError("settings must be a ProviderSettings")
        if self.status not in SETTINGS_STATUS_TEXTS:
            raise ValueError(f"unknown settings status {self.status!r}")

    @property
    def valid(self) -> bool:
        """Whether the file was usable (``False`` only for a malformed file)."""
        return self.status != SETTINGS_STATUS_INVALID

    @property
    def text(self) -> str:
        """The one operator-facing sentence for this outcome."""
        return SETTINGS_STATUS_TEXTS[self.status]


def default_provider_settings() -> ProviderSettings:
    """The documented defaults: OpenAI, Claude, Grok - the established wiring."""
    return ProviderSettings(
        **{
            key: ProviderSelection(provider)
            for key, provider in DEFAULT_PROVIDER_BY_ADVISOR.items()
        }
    )


def _selection_from_mapping(value: Any) -> ProviderSelection:
    """One slot's mapping, validated - or ``ValueError`` naming the field only."""
    if not isinstance(value, Mapping):
        raise ValueError("each advisor entry must be a JSON object")
    provider = value.get("provider", "disabled")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError('"provider" must be a non-empty string')
    model = value.get("model", "")
    if model is None:
        model = ""
    if not isinstance(model, str):
        raise ValueError('"model" must be a string')
    api_key = value.get("api_key", "")
    if api_key is None:
        api_key = ""
    if not isinstance(api_key, str):
        # the value is never echoed: an api_key must not reach a message
        raise ValueError('"api_key" must be a string')
    return ProviderSelection(provider=provider, model=model, api_key=api_key)


def settings_from_mapping(mapping: Any) -> ProviderSettings:
    """Build validated settings from a plain mapping (file JSON or GUI payload).

    Strict on structure and on the provider token, tolerant of a *missing* slot
    (which falls back to that slot's default): a file with one damaged advisor
    must not be able to silently change what another advisor uses. Unknown
    top-level keys are refused, so a hand-edited file cannot smuggle in state
    this module does not understand. Raises ``ValueError``; never renders a key.
    """
    if not isinstance(mapping, Mapping):
        raise ValueError("provider settings must be a JSON object")
    unknown = [key for key in mapping if key not in ADVISOR_KEYS]
    if unknown:
        raise ValueError(
            "provider settings may only name "
            f"{', '.join(ADVISOR_KEYS)}; got {len(unknown)} unknown entry(ies)"
        )
    selections = {
        key: _selection_from_mapping(mapping[key])
        for key in ADVISOR_KEYS
        if key in mapping
    }
    defaults = default_provider_settings()
    return ProviderSettings(
        **{
            key: selections.get(key, defaults.selection(key))
            for key in ADVISOR_KEYS
        }
    )


def load_provider_settings(
    path: Any = DEFAULT_PROVIDER_SETTINGS_PATH,
) -> ProviderSettingsLoad:
    """The remembered provider settings, or the defaults - never an exception.

    Every failure has a defined answer, because a preference must never be able
    to stop the panel from starting:

    * the file is missing or unreadable -> the documented defaults
      (:data:`SETTINGS_STATUS_DEFAULTS`);
    * the file is not JSON, is not an object, or carries an unusable entry ->
      the documented defaults **plus** an explicit
      :data:`SETTINGS_STATUS_INVALID` so the panel can say
      "provider settings invalid";
    * a usable file -> exactly what it says (:data:`SETTINGS_STATUS_LOADED`).
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError, TypeError):
        return ProviderSettingsLoad(
            default_provider_settings(), SETTINGS_STATUS_DEFAULTS
        )
    try:
        raw = json.loads(text)
        settings = settings_from_mapping(raw)
    except (ValueError, TypeError):
        return ProviderSettingsLoad(
            default_provider_settings(), SETTINGS_STATUS_INVALID
        )
    return ProviderSettingsLoad(settings, SETTINGS_STATUS_LOADED)


def save_provider_settings(path: Any, settings: Any) -> bool:
    """Write the provider configuration; ``False`` when it could not be written.

    The write is atomic: a temporary neighbour is written first and then moved
    over the target, so a crash or a full disk can never leave a truncated file
    that the next start would have to guess about. Nothing here raises, nothing
    here prints, and nothing here logs - a write failure costs the operator the
    save and nothing else.

    This is the **only** function that ever serializes an API key, and it writes
    it to the one local, Git-ignored preferences file. It never returns the key.
    """
    if not isinstance(settings, ProviderSettings):
        raise ValueError("settings must be a ProviderSettings")
    target = Path(path)
    temporary = target.with_name(f"{target.name}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(settings.to_file_dict(), indent=2, sort_keys=True) + "\n"
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
    except (OSError, ValueError, TypeError):
        try:
            temporary.unlink()
        except (OSError, ValueError, TypeError):
            pass
        return False
    return True
