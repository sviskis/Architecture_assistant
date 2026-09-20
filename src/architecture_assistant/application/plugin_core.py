"""Plugin core - a deterministic registry of named capability adapters.

**Registry != runtime policy.**

The registry stores *every* registered adapter for every capability (several
workers, several judges, several notifiers, several reporting formats, several
advisors, ...) and keeps one explicit default per capability. Which adapter is
*active* is a composition/configuration decision taken elsewhere; the registry
never forbids a second implementation of a capability just because the runtime
happens to use one.

Loading is explicit and deterministic: adapters are registered by name at
composition time. There is no filesystem scanning and no dynamic-import magic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..ports.capabilities import PluginCapability

__all__ = [
    "PluginError",
    "PluginNotFoundError",
    "PluginAlreadyRegisteredError",
    "PluginDefaultNotSetError",
    "PluginBinding",
    "PluginRegistry",
]


class PluginError(Exception):
    """Base class for plugin registry errors."""


class PluginNotFoundError(PluginError):
    """Raised when a named plugin - or any plugin for a capability - is missing."""


class PluginAlreadyRegisteredError(PluginError):
    """Raised when the same ``(capability, name)`` is registered twice."""


class PluginDefaultNotSetError(PluginError):
    """Raised when a capability has no default selection."""


@dataclass(frozen=True)
class PluginBinding:
    """One registry entry: a named adapter plus whether it is the default."""

    capability: PluginCapability
    name: str
    plugin: Any
    is_default: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.capability, PluginCapability):
            raise ValueError(
                f"capability must be a PluginCapability; got {self.capability!r}"
            )
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(
                f"name must be a non-empty string; got {self.name!r}"
            )


class PluginRegistry:
    """Deterministic registry of named adapters per capability.

    Rules:
    * any number of adapters may be registered per capability;
    * a duplicate ``(capability, name)`` is rejected;
    * the *first* adapter registered for a capability becomes its default, and
      later registrations never silently steal it;
    * the default can be changed at any time with :meth:`set_default`, without
      re-registering anything;
    * every listing is sorted by ``(capability, name)`` so results are stable.
    """

    def __init__(self) -> None:
        self._plugins: dict[tuple[PluginCapability, str], Any] = {}
        self._defaults: dict[PluginCapability, str] = {}

    # -- registration ----------------------------------------------------
    def register(
        self, capability: Any, name: Any, plugin: Any
    ) -> PluginBinding:
        """Register ``plugin`` under ``(capability, name)``."""
        resolved_capability = self._capability(capability)
        resolved_name = self._name(name)
        key = (resolved_capability, resolved_name)
        if key in self._plugins:
            raise PluginAlreadyRegisteredError(
                f"{resolved_capability.value} plugin {resolved_name!r} "
                "is already registered"
            )
        self._plugins[key] = plugin
        if resolved_capability not in self._defaults:
            self._defaults[resolved_capability] = resolved_name
        return self.binding(resolved_capability, resolved_name)

    # -- lookup ----------------------------------------------------------
    def binding(self, capability: Any, name: Any) -> PluginBinding:
        """Return the binding for a specific ``(capability, name)``."""
        resolved_capability = self._capability(capability)
        resolved_name = self._name(name)
        plugin = self._plugins.get((resolved_capability, resolved_name))
        if plugin is None:
            raise PluginNotFoundError(
                f"{resolved_capability.value} plugin {resolved_name!r} "
                "is not registered"
            )
        return PluginBinding(
            capability=resolved_capability,
            name=resolved_name,
            plugin=plugin,
            is_default=self._defaults.get(resolved_capability) == resolved_name,
        )

    def get(self, capability: Any, name: Any) -> Any:
        """Return a named adapter."""
        return self.binding(capability, name).plugin

    def has(self, capability: Any, name: Any) -> bool:
        """Whether a named adapter is registered (unknown input is ``False``)."""
        try:
            resolved_capability = self._capability(capability)
            resolved_name = self._name(name)
        except PluginError:
            return False
        return (resolved_capability, resolved_name) in self._plugins

    def names(self, capability: Any) -> tuple[str, ...]:
        """All registered adapter names for a capability, sorted."""
        resolved_capability = self._capability(capability)
        return tuple(
            sorted(
                name
                for (registered, name) in self._plugins
                if registered is resolved_capability
            )
        )

    def list(self, capability: Any = None) -> tuple[PluginBinding, ...]:
        """All bindings, deterministically sorted by ``(capability, name)``."""
        if capability is None:
            keys = tuple(self._plugins)
        else:
            resolved_capability = self._capability(capability)
            keys = tuple(
                key for key in self._plugins if key[0] is resolved_capability
            )
        ordered = sorted(keys, key=lambda key: (key[0].value, key[1]))
        return tuple(self.binding(cap, name) for cap, name in ordered)

    # -- default selection (runtime policy lives outside the registry) ----
    def default_name(self, capability: Any) -> str:
        """Name of the currently selected adapter for a capability."""
        resolved_capability = self._capability(capability)
        name = self._defaults.get(resolved_capability)
        if name is None:
            raise PluginDefaultNotSetError(
                f"{resolved_capability.value} has no registered plugin to "
                "default to"
            )
        return name

    def get_default(self, capability: Any) -> Any:
        """The currently selected (active) adapter for a capability."""
        return self.get(capability, self.default_name(capability))

    def set_default(self, capability: Any, name: Any) -> None:
        """Select the active adapter without re-registering it."""
        resolved_capability = self._capability(capability)
        resolved_name = self._name(name)
        if (resolved_capability, resolved_name) not in self._plugins:
            raise PluginNotFoundError(
                f"cannot default {resolved_capability.value} to unregistered "
                f"plugin {resolved_name!r}"
            )
        self._defaults[resolved_capability] = resolved_name

    # -- internals -------------------------------------------------------
    @staticmethod
    def _capability(value: Any) -> PluginCapability:
        if isinstance(value, PluginCapability):
            return value
        if isinstance(value, str):
            try:
                return PluginCapability(value)
            except ValueError:
                pass
        valid = ", ".join(member.value for member in PluginCapability)
        raise PluginError(
            f"unknown capability {value!r}; expected one of ({valid})"
        )

    @staticmethod
    def _name(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise PluginError(
                f"plugin name must be a non-empty string; got {value!r}"
            )
        return value

